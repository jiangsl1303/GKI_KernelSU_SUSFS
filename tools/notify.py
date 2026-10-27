#!/usr/bin/env python3
"""Server酱推送（里程碑 / 构建结果）。

用法：
  python tools/notify.py "标题" "正文（支持 markdown）"
  python tools/notify.py --title T --body B [--key SCT...]

sendkey 查找顺序：--key > 环境变量 SERVERCHAN_SENDKEY > 最近的 .secrets/serverchan.env
（端点用 sctapi.ftqq.com；ft07 在本机被代理拦，实测 502）
"""

from __future__ import annotations

import argparse
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ENDPOINTS = ["https://sctapi.ftqq.com/{key}.send", "https://{key}.push.ft07.com/send"]


def find_key(explicit: str = "") -> str:
    if explicit:
        return explicit.strip()
    import os

    if os.environ.get("SERVERCHAN_SENDKEY"):
        return os.environ["SERVERCHAN_SENDKEY"].strip()
    here = Path(__file__).resolve().parent.parent
    for base in (here, here.parent, here.parent.parent, here.parent.parent.parent):
        path = base / ".secrets" / "serverchan.env"
        if path.exists():
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if line.startswith("SERVERCHAN_SENDKEY="):
                    return line.split("=", 1)[1].strip()
    return ""


def push(key: str, title: str, body: str) -> int:
    payload = urllib.parse.urlencode({"title": title, "desp": body}).encode()
    last = ""
    for template in ENDPOINTS:
        url = template.format(key=key)
        req = urllib.request.Request(url, data=payload, method="POST")
        req.add_header("Content-Type", "application/x-www-form-urlencoded")
        req.add_header("User-Agent", "curl/8")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                text = resp.read().decode("utf-8", "replace")
            print(f"[+] 推送成功 {url.split('/')[2]}: {text[:300]}")
            return 0
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
            print(f"[!] {url.split('/')[2]} 失败: {exc}")
    print(f"[FAIL] 所有端点都失败: {last}")
    return 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("title", nargs="?", default="", help="标题")
    ap.add_argument("body", nargs="?", default="", help="正文（markdown）")
    ap.add_argument("--key", default="", help="Server酱 sendkey")
    args = ap.parse_args()

    title = (args.title or "通知").strip()
    body = args.body or title

    key = find_key(args.key)
    if not key:
        print("[!] 没有 sendkey，跳过推送（--key 或 .secrets/serverchan.env）")
        return 0
    return push(key, title[:100], body)


if __name__ == "__main__":
    sys.exit(main())
