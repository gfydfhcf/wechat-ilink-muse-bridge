# Contributing

Thanks for your interest. This is a small, single-maintainer project —
contributions are welcome, and keeping them small and focused is what
gets them merged.

> 中文读者：流程就是下面这些——先跑通、提 PR 前先开 issue 聊大改动、
> 核心客户端只用标准库、绝不要提交数据目录（里面是登录凭证）。

## Set up

```bash
git clone https://github.com/gfydfhcf/wechat-ilink-muse-bridge
cd wechat-ilink-muse-bridge
pip install -r requirements.txt     # only hard dependency: cryptography
export ILINK_DATA_DIR=/tmp/ilink-dev-data
python3 ilink.py qrcode             # then: python3 ilink.py login <id>
```

Python 3.10+. For the drivers, see `AGENTS.md` (Muse hook setup) and
`daemon.py` (standalone).

## Run the tests

```bash
python3 -m unittest discover -s tests -v
```

All tests are offline — network and CDN behavior is mocked. Every
change should keep the whole suite green, and bug fixes should come
with a test that fails without the fix.

## Ground rules

- **The core client stays dependency-free.** `ilink.py` uses only the
  Python standard library, plus `cryptography` for the media CDN's
  AES. New runtime dependencies will not be merged.
- **Never commit the data directory.** `session.json` in it is a login
  credential (see `SECURITY.md`). Strip tokens and user IDs from any
  logs or screenshots you attach.
- **The protocol traps are documented in code comments for a reason**
  (the two enum/encoding mismatches around media upload, the
  message-vs-media type tables). If you touch that code, keep the
  comments accurate.
- **One topic per pull request.** For anything larger than a small
  fix, open an issue first so the direction can be agreed before you
  write the code.
- Match the existing style: comments explain *why*, commit messages
  say what changed and why.

## Reporting bugs

Use the bug report template, and include your driver (hook / daemon /
Docker), Python version, and the relevant log lines — with tokens and
session data removed.
