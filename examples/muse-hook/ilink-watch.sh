#!/usr/bin/env bash
# iLink 微信分身：长轮询收消息（hook 版）
# 每一轮脚本运行 = 一次 getupdates 长轮询（服务端 hold 最多 35s，有消息立刻返回）。
# 有新消息 -> 写入 pending 队列并 wake，由 worker agent 生成回复；
# 无消息/出错 -> silent。
# poll 是两段式的：ilink.py poll 只拉取、不推进游标不记去重 ID；本脚本把
# 批次原子落盘 pending 之后才调 ilink.py commit 提交。任何一步崩溃，
# 消息都会在下一轮被重新拉到——宁可重复，绝不丢失。
# 告警闭环（alert-state.json 状态机）：
#   - token 过期：首次立即 wake 报一次，之后每小时复报一次（payload 带 repeat/hours）
#   - 轮询连续失败：满 10 分钟才报（防抖动误报），之后每小时复报一次
#   - 恢复：曾告警过的异常恢复正常后 wake 一次 recovered（报好不只报坏）；
#     短暂抖动未告警的静默清除状态
#   - worker 卡住：processing 超 10 分钟被回收时记 stuck 起点，积压超 10 分钟
#     随 pending 的 wake 带 stuck_alert（每小时至多一次）
set -euo pipefail
source "$HATCH_HOOK_RUNTIME"

# Paths are parameterized for this public example. In production these point
# at the bridge's data dir and the hook state dir.
BRIDGE=${ILINK_DATA_DIR:-$HOME/wechat-ilink-data}
STATE_DIR=${ILINK_HOOK_STATE:-$HOME/.ilink-hook-state}
mkdir -p "$STATE_DIR"
PENDING=$STATE_DIR/ilink-pending.json
PROC=$STATE_DIR/ilink-processing.json
RESCAN=$BRIDGE/rescan_notified
POLLLOG=$BRIDGE/poll.log
ALERT=$BRIDGE/alert-state.json
DRY=${HATCH_HOOK_DRY_RUN:-0}
NOW=$(date +%s)

now() { date '+%Y-%m-%d %H:%M:%S'; }

# 告警状态读写（jq，原子写；dry-run 不写状态，保持纯净）
astate() { jq -r ".$1 // 0" "$ALERT" 2>/dev/null || echo 0; }
aset() {
  [ "$DRY" = "1" ] && return 0
  local tmp="$ALERT.tmp.$$"
  if [ -f "$ALERT" ]; then
    jq --argjson v "$2" ".$1 = \$v" "$ALERT" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  else
    jq -n --argjson v "$2" "{\"$1\": \$v}" > "$tmp" 2>/dev/null || echo '{}' > "$tmp"
  fi
  mv "$tmp" "$ALERT" 2>/dev/null || true
}

# 崩溃恢复：上一个 worker 超过 10 分钟没处理完，把消息退回 pending
# 崩溃恢复：上一个 worker 超过 10 分钟没处理完，把消息退回 pending
# 注意：必须用 -cmin（inode 变更时间）而非 -mmin（内容修改时间）：
# worker 用 mv 认领队列时 mtime 保留为消息落盘时间，若消息在 pending 里
# 积压超过 10 分钟，认领瞬间就会被误判过期、打回 pending 形成乒乓循环。
# mv 会刷新 ctime，ctime 才是真正的“被认领时间”。
# 待处理队列里还有批次时不回收：先处理排队中的（它更新），卡住的下一轮再说。
# 否则 mv 会覆盖掉尚未处理的新批次——覆盖即永久丢失。
pending_msgs=0
if [ -s "$PENDING" ]; then
  pending_msgs=$(jq '[.msgs[]?] | length' "$PENDING" 2>/dev/null || echo 0)
fi
if [ -f "$PROC" ] && [ "${pending_msgs:-0}" -eq 0 ] && [ -n "$(find "$PROC" -cmin +10 2>/dev/null || true)" ]; then
  mv "$PROC" "$PENDING"
  log "recovered stale processing file to pending"
  # 卡住计时：能被回收说明这一轮处理至少已卡了 10 分钟，起点回拨 600 秒
  if [ "$(astate stuck_since)" -eq 0 ]; then
    aset stuck_since $((NOW - 600))
  fi
fi

# 有未处理的消息（上一轮拉到、agent 还没吃完）：直接 wake，不再拉新的
if [ -s "$PENDING" ]; then
  cnt=$(jq '.msgs | length' "$PENDING" 2>/dev/null || echo 0)
  if [ "$cnt" -gt 0 ] 2>/dev/null; then
    log "pending $cnt messages waiting"
    # Leftover batches also light the typing indicator immediately (owner only)
    owner=$(jq -r '.owner_id // empty' "$BRIDGE/owner.json" 2>/dev/null || true)
    sender=$(jq -r '.msgs[0].from_user_id // empty' "$PENDING" 2>/dev/null || true)
    if [ -n "$owner" ] && [ "$sender" = "$owner" ]; then
      python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" typing "$sender" start >/dev/null 2>&1 || true
    fi
    # 卡住告警：积压起点距今 >=10 分钟且距上次告警 >=1 小时，随 wake 带出
    extra=""
    stuck_since=$(astate stuck_since)
    stuck_last=$(astate stuck_last)
    if [ "$stuck_since" -gt 0 ] && [ $((NOW - stuck_since)) -ge 600 ] \
       && [ $((NOW - stuck_last)) -ge 3600 ]; then
      aset stuck_last "$NOW"
      extra=",\"stuck_alert\":true,\"stuck_minutes\":$(( (NOW - stuck_since) / 60 ))"
    fi
    wake "pending_wechat_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"$extra}"
  else
    silent "pending file empty"
  fi
  # 分支已结束（wake/silent 为最后调用）
else
  # 长轮询一轮（服务端 hold 住最多 35s）
  out=$(python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" poll 2>/dev/null || true)

  if [[ "$out" == NO_TOKEN* ]] || [[ "$out" == TOKEN_EXPIRED* ]]; then
    exp_since=$(astate exp_since)
    exp_last=$(astate exp_last)
    if [ "$exp_since" -eq 0 ]; then
      # 首次过期：立即报一次（rescan_notified 标记保留作兼容，成功轮询时清除）
      aset exp_since "$NOW"
      aset exp_last "$NOW"
      if [ "$DRY" != "1" ]; then
        touch "$RESCAN"
        wake "token_expired" "{}"
      else
        silent "dry-run: would set expiry state and wake token_expired"
      fi
    elif [ $((NOW - exp_last)) -ge 3600 ]; then
      # 长时间未恢复：每小时复报一次
      aset exp_last "$NOW"
      if [ "$DRY" != "1" ]; then
        wake "token_expired" "{\"repeat\":true,\"hours\":$(( (NOW - exp_since) / 3600 ))}"
      else
        silent "dry-run: would repeat token_expired alert"
      fi
    else
      silent "token expired, repeat alert throttled"
    fi
  elif [[ "$out" == POLL_ERROR* ]] || ! printf '%s' "$out" | jq -e 'type == "object" and has("msgs")' >/dev/null 2>&1; then
    # POLL_ERROR，或 poll 进程硬崩溃（空输出/非法 JSON/缺 msgs 字段）。
    # 崩溃若不进本分支，会被误判成"没有新消息"、故障永远不告警。
    if [ "$DRY" != "1" ]; then
      echo "[$(now)] $(printf '%s' "${out:-POLL_CRASH:empty-or-invalid-output}" | head -c 200)" >> "$POLLLOG"
    fi
    err_since=$(astate err_since)
    if [ "$err_since" -eq 0 ]; then
      aset err_since "$NOW"
      err_since=$NOW
    fi
    err_last=$(astate err_last)
    err_dur=$((NOW - err_since))
    if [ "$err_dur" -ge 600 ] && { [ "$err_last" -eq 0 ] || [ $((NOW - err_last)) -ge 3600 ]; }; then
      # 连续失败满 10 分钟才报（抖动通常几分钟内自愈），之后每小时复报
      aset err_last "$NOW"
      if [ "$DRY" != "1" ]; then
        wake "poll_failing" "{\"minutes\":$((err_dur / 60))}"
      else
        silent "dry-run: would wake poll_failing"
      fi
    else
      silent "poll error"
    fi
  else
    # 轮询成功：先做恢复记账（曾告警过的异常恢复了要报平安）
    recovered=""
    exp_since=$(astate exp_since)
    err_since=$(astate err_since)
    # 卡住事件收尾：队列已排空（无 pending 才进本分支）且无人处理中，清状态
    if [ ! -f "$PROC" ] && [ "$(astate stuck_since)" -gt 0 ]; then
      aset stuck_since 0
      aset stuck_last 0
    fi
    if [ "$exp_since" -gt 0 ]; then
      recovered="{\"kind\":\"token\",\"minutes\":$(( (NOW - exp_since) / 60 ))}"
      aset exp_since 0
      aset exp_last 0
      if [ "$DRY" != "1" ]; then rm -f "$RESCAN"; fi
    elif [ "$err_since" -gt 0 ]; then
      if [ "$(astate err_last)" -gt 0 ]; then
        recovered="{\"kind\":\"poll_error\",\"minutes\":$(( (NOW - err_since) / 60 ))}"
      fi
      aset err_since 0
      aset err_last 0
    fi
    cnt=$(printf '%s' "$out" | jq '[.msgs[]?] | length' 2>/dev/null || echo 0)
    commit_json=$(printf '%s' "$out" | jq -c '.commit // empty' 2>/dev/null || true)
    # 提交本轮 poll（推进游标 + 记 seen）：只能在批次可靠落盘之后调用。
    # 失败只记日志、不中断——下轮会重拉这批消息，宁可重复，绝不丢失。
    do_commit() {
      if [ "$DRY" != "1" ] && [ -n "$commit_json" ]; then
        printf '%s' "$commit_json" | python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" commit >/dev/null 2>&1 \
          || echo "[$(now)] COMMIT_FAILED" >> "$POLLLOG"
      fi
    }
    if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
      if [ "$DRY" = "1" ]; then
        # dry 不落盘、不提交：消息留给正式轮询去拉，避免重复入队
        silent "dry-run: $cnt messages fetched, left uncommitted for live poll"
      else
        # 原子写入 pending（只写 msgs，保持队列文件既有格式）
        tmp="$PENDING.tmp.$$"
        printf '%s' "$out" | jq -c '{msgs}' > "$tmp"
        mv "$tmp" "$PENDING"
        log "received $cnt messages"
        # Light the "typing..." indicator the moment the batch lands (owner
        # only) instead of waiting for the worker agent to boot — the worker's
        # own typing start then just acts as a keep-alive refresh.
        owner=$(jq -r '.owner_id // empty' "$BRIDGE/owner.json" 2>/dev/null || true)
        sender=$(printf '%s' "$out" | jq -r '.msgs[0].from_user_id // empty' 2>/dev/null || true)
        if [ -n "$owner" ] && [ "$sender" = "$owner" ]; then
          python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" typing "$sender" start >/dev/null 2>&1 || true
        fi
        do_commit
        rec_extra=""
        if [ -n "$recovered" ]; then rec_extra=",\"recovered\":$recovered"; fi
        wake "new_wechat_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"$rec_extra}"
      fi
    elif [ -n "$recovered" ]; then
      do_commit
      if [ "$DRY" != "1" ]; then
        wake "recovered" "$recovered"
      else
        silent "dry-run: would wake recovered"
      fi
    else
      do_commit
      silent "no new messages"
    fi
  fi
fi
