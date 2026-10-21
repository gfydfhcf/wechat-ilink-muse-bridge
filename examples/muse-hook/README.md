# Muse hook 变体：把“长轮询”塞进 Muse 的 hook 机制里

[中文](README.md) | [English](README.en.md)

`daemon.py` 是给普通环境用的常驻长轮询循环。但 Muse 运行时里已经有
现成的 agent 调度，不需要再养一个调 LLM 的进程——本目录是生产环境
实际在用的另一种接法：

## 结构

```
微信用户 ──消息──> iLink 服务器
                      │  getupdates（长轮询，服务端 hold ≤35s）
                      ▼
              ilink-watch.sh        ← Muse hook 脚本，每 5s 跑一轮，
                      │               每轮就是一次长轮询
             有新消息 │  先落盘，再 wake
                      ▼
        ilink-pending.json          ← 待处理队列（原子写入，断电不丢）
                      │  worker 用 mv 原子认领 → ilink-processing.json
                      ▼
              Muse worker agent     ← 发送前复查队列、合并连发，只回一条；
                                      超过 10 分钟没处理完的批次会被
                                      脚本退回 pending 重新唤醒
```

## 为什么这么绕

- **不丢消息**：hook 脚本先把消息落盘再唤醒 agent。agent 崩溃/超时，
  消息还在队列里，下一轮脚本会再次唤醒。
- **不重复回复**：认领用 `mv`（原子操作），多个重复唤醒里只有一个
  能拿到队列，其余安静退出。
- **连发合并**：用户连发几条时 iLink 会拆成多个响应。worker 不做固定
  等待，而是**发送前复查**：回复组织好、真正发出之前再看一眼队列，
  有新消息就合并进来、按最新上下文重新组织回复，最后只回一条。
- **"正在输入"秒亮**：消息落盘那一刻，脚本核对发信人是主人后立即点亮
  typing 指示，不等 worker 冷启动；worker 侧的点亮只作续期。
- **token 过期只通知一次**：脚本发现 `TOKEN_EXPIRED` 时创建
  `rescan_notified` 标记并唤醒 agent 通知用户重扫；标记在就不再打扰。

脚本里的路径已参数化（`ILINK_DATA_DIR`、`ILINK_HOOK_STATE`、`ILINK_CLIENT`），
`$HATCH_HOOK_RUNTIME` 是 Muse hook 运行时提供的环境，非 Muse 环境请用
仓库根目录的 `daemon.py`。
