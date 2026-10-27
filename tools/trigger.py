#!/usr/bin/env python3
"""触发仓库里的 GKI Latest 工作流（workflow_dispatch）。

凭据优先级：--token  >  环境变量 GH_TOKEN / GITHUB_TOKEN  >  工作区 .secrets/gh_token
没有凭据时只打印「你要在网页上点哪些选项」，不报错退出，方便手动跑。

示例：
  python tools/trigger.py --families android14-6.1 --os-patch-level lts
  python tools/trigger.py --families all --os-patch-level all --features SUSFS,Perf
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

DEFAULT_REPO = "jiangsl1303/GKI_KernelSU_SUSFS"
DEFAULT_WORKFLOW = "gki-latest.yml"
TOKEN_HINTS = [".secrets/gh_token", ".gh_token"]
API = "https://api.github.com"


def find_token(explicit: str = "") -> str:
    if explicit:
        return explicit.strip()
    for env in ("GH_TOKEN", "GITHUB_TOKEN"):
        if os.environ.get(env):
            return os.environ[env].strip()
    here = Path(__file__).resolve().parent.parent
    for rel in TOKEN_HINTS:
        for base in (here, here.parent, here.parent.parent):
            path = base / rel
            if path.exists():
                return path.read_text(encoding="utf-8").strip()
    return ""


def api(method: str, url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "gki-latest-trigger")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            body = resp.read().decode()
            return json.loads(body) if body.strip() else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")[:800]
        raise SystemExit(f"[!] GitHub API {exc.code} {url}\n{detail}") from exc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    ap.add_argument("--ref", default="main")
    ap.add_argument("--families", default="all")
    ap.add_argument("--os-patch-level", default="lts")
    ap.add_argument("--features", default="SUSFS")
    ap.add_argument("--page-size", default="4k", choices=["4k", "16k"])
    ap.add_argument("--brand-name", default="Wild")
    ap.add_argument("--use-cache", action="store_true")
    ap.add_argument("--no-notify", action="store_true")
    ap.add_argument("--token", default="")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    inputs = {
        "families": args.families,
        "os_patch_level": args.os_patch_level,
        "features": args.features,
        "page_size": args.page_size,
        "brand_name": args.brand_name,
        "use_cache": "true" if args.use_cache else "false",
        "notify_serverchan": "false" if args.no_notify else "true",
    }

    print("[*] 目标:", f"{args.repo} / {args.workflow} @ {args.ref}")
    for key, value in inputs.items():
        print(f"    {key} = {value}")

    token = find_token(args.token)
    if args.dry_run or not token:
        if not token:
            print("\n[!] 没有 GitHub 凭据（GH_TOKEN / .secrets/gh_token）—— 只在网页上手动触发：")
            print(f"    1) 打开 https://github.com/{args.repo}/actions/workflows/{args.workflow}")
            print("    2) Run workflow → 按上面列出的值填写 → Run")
        return 0

    api(
        "POST",
        f"{API}/repos/{args.repo}/actions/workflows/{args.workflow}/dispatches",
        token,
        {"ref": args.ref, "inputs": inputs},
    )
    print("[+] 已触发，等待 run 出现…")

    import time

    for _ in range(10):
        time.sleep(6)
        runs = api(
            "GET",
            f"{API}/repos/{args.repo}/actions/workflows/{args.workflow}/runs?event=workflow_dispatch&per_page=5",
            token,
        )
        for run in runs.get("workflow_runs", []):
            if run.get("head_branch") == args.ref:
                print(f"[+] run id = {run['id']}")
                print(f"    {run['html_url']}")
                Path("/tmp/gki_run_id").write_text(str(run["id"]), encoding="utf-8")
                return 0
    print("[!] 已触发但没查到 run，请到 Actions 页面确认")
    return 0


if __name__ == "__main__":
    sys.exit(main())
