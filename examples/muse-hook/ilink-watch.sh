#!/usr/bin/env bash
# iLink 微信分身：长轮询收消息（hook 版）
# 每一轮脚本运行 = 一次 getupdates 长轮询（服务端 hold 最多 35s，有消息立刻返回）。
# 有新消息 -> 写入 pending 队列并 wake，由 worker agent 生成回复；
# 无消息/出错 -> silent。
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
DRY=${HATCH_HOOK_DRY_RUN:-0}

now() { date '+%Y-%m-%d %H:%M:%S'; }

# 崩溃恢复：上一个 worker 超过 10 分钟没处理完，把消息退回 pending
# 崩溃恢复：上一个 worker 超过 10 分钟没处理完，把消息退回 pending
# 注意：必须用 -cmin（inode 变更时间）而非 -mmin（内容修改时间）：
# worker 用 mv 认领队列时 mtime 保留为消息落盘时间，若消息在 pending 里
# 积压超过 10 分钟，认领瞬间就会被误判过期、打回 pending 形成乒乓循环。
# mv 会刷新 ctime，ctime 才是真正的“被认领时间”。
if [ -f "$PROC" ] && [ -n "$(find "$PROC" -cmin +10 2>/dev/null || true)" ]; then
  mv "$PROC" "$PENDING"
  log "recovered stale processing file to pending"
fi

# 有未处理的消息（上一轮拉到、agent 还没吃完）：直接 wake，不再拉新的
if [ -s "$PENDING" ]; then
  cnt=$(jq 'length' "$PENDING" 2>/dev/null || echo 0)
  if [ "$cnt" -gt 0 ] 2>/dev/null; then
    log "pending $cnt messages waiting"
    # Leftover batches also light the typing indicator immediately (owner only)
    owner=$(jq -r '.owner_id // empty' "$BRIDGE/owner.json" 2>/dev/null || true)
    sender=$(jq -r '.msgs[0].from_user_id // empty' "$PENDING" 2>/dev/null || true)
    if [ -n "$owner" ] && [ "$sender" = "$owner" ]; then
      python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" typing "$sender" start >/dev/null 2>&1 || true
    fi
    wake "pending_wechat_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"}"
  else
    silent "pending file empty"
  fi
  # 分支已结束（wake/silent 为最后调用）
else
  # 长轮询一轮（服务端 hold 住最多 35s）
  out=$(python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" poll 2>/dev/null || true)

  if [[ "$out" == NO_TOKEN* ]] || [[ "$out" == TOKEN_EXPIRED* ]]; then
    if [ ! -e "$RESCAN" ]; then
      if [ "$DRY" != "1" ]; then
        touch "$RESCAN"
        wake "token_expired" "{}"
      else
        silent "dry-run: would create rescan marker and wake token_expired"
      fi
    else
      silent "token expired already notified"
    fi
  elif [[ "$out" == POLL_ERROR* ]]; then
    if [ "$DRY" != "1" ]; then
      echo "[$(now)] $out" >> "$POLLLOG"
    fi
    silent "poll error"
  else
    cnt=$(printf '%s' "$out" | jq '[.msgs[]?] | length' 2>/dev/null || echo 0)
    if [ "${cnt:-0}" -gt 0 ] 2>/dev/null; then
      # 原子写入 pending（消息已从服务端消费，必须落盘防丢）
      tmp="$PENDING.tmp.$$"
      printf '%s' "$out" > "$tmp"
      mv "$tmp" "$PENDING"
      log "received $cnt messages"
      # Light the "typing..." indicator the moment the batch lands (owner
      # only) instead of waiting for the worker agent to boot — the worker's
      # own typing start then just acts as a keep-alive refresh.
      owner=$(jq -r '.owner_id // empty' "$BRIDGE/owner.json" 2>/dev/null || true)
      sender=$(printf '%s' "$out" | jq -r '.msgs[0].from_user_id // empty' 2>/dev/null || true)
      if [ "$DRY" != "1" ] && [ -n "$owner" ] && [ "$sender" = "$owner" ]; then
        python3 "${ILINK_CLIENT:-$BRIDGE/ilink.py}" typing "$sender" start >/dev/null 2>&1 || true
      fi
      if [ "$DRY" != "1" ]; then
        wake "new_wechat_messages" "{\"count\":$cnt,\"pending_file\":\"$PENDING\"}"
      else
        silent "dry-run: $cnt messages saved to pending, no wake"
      fi
    else
      silent "no new messages"
    fi
  fi
fi
