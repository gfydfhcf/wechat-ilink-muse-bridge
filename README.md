# wechat-ilink-muse-bridge

**把 Muse（Meta 的个人 AI 助理）接进微信。**

基于腾讯官方 iLink / ClawBot 协议（`ilinkai.weixin.qq.com`），扫码授权后，
你的 Muse 分身就能在微信里实时收发消息：你发微信，它秒回；它也能
主动给你推送提醒（快递、服务器告警、日程……），实现 App + 微信双通道。

核心客户端 `ilink.py` 不依赖任何框架，单独拿走就是一个极简 iLink 客户端；
仓库同时给出两种长轮询接法：独立 `daemon.py`，和生产在用的 Muse hook 方案。

## 特性

- **官方协议，扫码登录** — 不用逆向、不用挂个人号，走腾讯 iLink 官方通道
- **长轮询实时收消息** — 服务端 hold 住连接最长 35s，有消息立即返回，
  延迟秒级（同类项目清一色这个做法，本仓库的参数与退避策略可直接抄）
- **连发消息合并** — 用户连发几条会被 iLink 拆成多个响应；本仓库会
  攒一攒、合并成一轮发言，只回一条。**调研过的所有同类 SDK 都不做这个**，
  直接逐条回复的机器人会显得很机械
- **"正在输入"状态** — 回复生成期间自动亮起 typing 指示
  （`getconfig` 取 `typing_ticket` + `sendtyping` 开关，票据缓存 9 分钟），
  等待不干等，对方能看到机器人"活着"
- **主动推送** — 协议“文档”没写，但 `sendmessage` 配合缓存的
  `context_token` 可以主动给用户发消息（token 失效时有无 token 兜底重试）
- **媒体下载断点续传** — 图片/文件走加密 CDN，下载中断用 HTTP Range
  续传 + 指数退避重试（弱网下整图重下经常死在 90%）
- **发送防重复** — 发送失败绝不手动重试（服务端偶尔收了消息却不回
  回执，盲目重试 = 用户收到两条）
- **token 过期只吵一次** — 过期后落一个标记文件，只通知用户重扫一次

## 和同类项目的区别

GitHub 上 iLink 项目已经不少：SDK 类（[corespeed-io/wechatbot](https://github.com/corespeed-io/wechatbot)、
[epiral/weixin-bot](https://github.com/epiral/weixin-bot)）给开发者当库用；
Claude Code 类（[crazynomad/weixin-claude-bot](https://github.com/crazynomad/weixin-claude-bot)）
是拿微信遥控编程工具。

这个仓库接的是 **Muse 个人助理本体**——一个有长期记忆、会管目标和
提醒、能主动汇报的助理，而不是一个编程 CLI 或裸 LLM API。所以它的
重点不在“库设计”，而在**一个真实助理跑在微信上会踩的坑**：连发合并、
防重复发送、过期单次提醒、消息队列不丢不重。欢迎对着抄。

## 架构

```
微信 ──> iLink 服务器 ──getupdates 长轮询──> ilink.py poll
                                                  │ 新消息落盘
                                                  ▼
                                          pending 队列 ──> 助理/worker
                                                  │ 合并连发、生成回复
                                                  ▼
                                          ilink.py send ──> 微信
主动推送：任意程序 ──> ilink.py proactive（缓存 context_token）──> 微信
```

两种轮询驱动任选其一：

| 方式 | 适合 | 入口 |
| --- | --- | --- |
| 独立守护进程 | 任何服务器/VPS | `daemon.py` |
| Muse hook | Muse 运行时（生产在用） | `examples/muse-hook/` |

## 快速开始

```bash
pip install -r requirements.txt

# 1. 扫码登录（打印二维码图片路径，用微信扫）
python3 ilink.py qrcode
python3 ilink.py login <qrcode-id>     # 登录态存入 data/session.json

# 2. 起长轮询（--echo 是演示 handler，收到什么回什么）
python3 daemon.py --echo

# 之后给机器人发条微信试试——连发两条，只会收到一条合并回复
```

单条命令速查：

```bash
python3 ilink.py poll                                   # 拉一轮新消息（JSON）
python3 ilink.py send <user_id> <context_token> -       # 发回复（正文走 stdin）
python3 ilink.py proactive <user_id> -                  # 主动推送（正文走 stdin）
python3 ilink.py typing <user_id> <start|stop>          # 显示/取消"正在输入"
```

数据目录默认 `./data/`，可用环境变量 `ILINK_DATA_DIR` 覆盖。

## 已知限制（先说丑话）

- **登录态寿命有限**：社区经验约 24 小时量级，过期需重新扫码
  （本仓库只做了“过期只通知一次”，重扫还得人来）。
- **只能收到主动发给机器人的消息**：iLink 是独立的机器人身份，
  看不到你主号的消息、通讯录、朋友圈——官方协议里根本没有这些端点，
  谁说有谁就是在用逆向协议（有封号风险）。
- **出口网络要稳**：长轮询连接会被跨国链路偶发切断，客户端需退避重连
  （本仓库已处理，日志里 `POLL_ERROR` 是常客，不影响使用）。

## 安全说明

`data/session.json` 是**登录凭证**，等同于密码：只留在本地，已写进
`.gitignore`，**永远不要提交、不要分享**。聊天记录、联系人缓存同理。

## License

MIT
