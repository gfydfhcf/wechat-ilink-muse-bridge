# Security Policy

## Supported versions

Only the latest `main` branch is supported. There are no versioned
releases yet; if you deploy this project, track `main`.

## Reporting a vulnerability

**Do not open a public issue for a security vulnerability.**

Use GitHub's private vulnerability reporting instead: go to the
repository's **Security** tab → **Report a vulnerability**. Include
what the problem is, how to reproduce it, and what an attacker could
gain. You should get a reply within a few days.

## The one rule that matters most here

The bridge's data directory (`ILINK_DATA_DIR`, default `./data/`)
contains `session.json` with the bot's **login token — a credential**.
Anyone holding it can operate the bot session.

- Never commit the data directory, a session file, a QR login image,
  or unredacted chat logs to git, an issue, or a pull request.
- When reporting bugs, strip tokens, session contents and user IDs
  from logs before pasting them anywhere.
- If you accidentally published a token, treat it as compromised:
  log the bot in again (fresh QR scan) as soon as possible and remove
  the leaked copy, including from git history where feasible.

---

# 安全政策（中文摘要）

- 发现安全漏洞**不要公开提 issue**，请走仓库 Security 标签页的
  私密漏洞报告通道。
- 数据目录里的 `session.json` 是登录凭证：绝不要把数据目录、
  session 文件、登录二维码、未打码的聊天记录提交到仓库或贴到
  issue/PR 里。token 一旦泄露，立刻重新扫码登录并清理泄露副本。
