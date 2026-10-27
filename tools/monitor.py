#!/usr/bin/env python3
"""监控一次 Actions run：job 级进度 → 结论 → 产物清单（可下载）。

用法：
  python tools/monitor.py 12345678            # 轮询到结束
  python tools/monitor.py 12345678 --interval 60 --download outputs
  python tools/monitor.py --latest           # 自动找最近一次 GKI Latest run

注意：artifact 下载会 302 到 Azure 签名 URL，必须跟随跳转且**不要**带上 Authorization，
否则 401 —— 所以这里用 curl -sL 而不是 urllib。
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from trigger import API, DEFAULT_REPO, DEFAULT_WORKFLOW, find_token  # noqa: E402


def api(url: str, token: str) -> dict:
    req = urllib.request.Request(url)
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "gki-latest-monitor")
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            return json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raise SystemExit(f"[!] GitHub API {exc.code} {url}\n{exc.read().decode(errors='replace')[:400]}")


def latest_run(repo: str, workflow: str, token: str) -> int:
    data = api(f"{API}/repos/{repo}/actions/workflows/{workflow}/runs?per_page=1", token)
    runs = data.get("workflow_runs") or []
    if not runs:
        raise SystemExit("[!] 没有找到任何 run")
    return int(runs[0]["id"])


def show(repo: str, run_id: int, token: str) -> tuple[str, str]:
    run = api(f"{API}/repos/{repo}/actions/runs/{run_id}", token)
    jobs = api(f"{API}/repos/{repo}/actions/runs/{run_id}/jobs?per_page=100", token).get("jobs", [])
    print(f"\n=== run {run_id} | {run['status']} / {run.get('conclusion')} | {run['created_at']} ===")
    for job in jobs:
        concl = job.get("conclusion") or job.get("status")
        mark = {"success": "OK ", "failure": "FAIL", "skipped": "SKIP"}.get(concl, " .. ")
        print(f" [{mark}] {job['name']}")
        for step in job.get("steps", []):
            if step.get("conclusion") == "failure":
                print(f"          ↳ 失败步骤: {step['number']} {step['name']}")
    return run["status"], (run.get("conclusion") or "")


def artifacts(repo: str, run_id: int, token: str) -> list[dict]:
    data = api(f"{API}/repos/{repo}/actions/runs/{run_id}/artifacts?per_page=100", token)
    return data.get("artifacts", [])


def download(repo: str, run_id: int, token: str, dest: Path) -> None:
    items = artifacts(repo, run_id, token)
    if not items:
        print("[!] 没有产物")
        return
    dest.mkdir(parents=True, exist_ok=True)
    print(f"\n=== 产物 {len(items)} 个 → {dest} ===")
    for item in items:
        name = item["name"]
        if not shutil.which("curl"):
            print(f"[!] 没有 curl，跳过 {name}")
            continue
        out = dest / f"{name}.zip"
        cmd = [
            "curl", "-sSL", "--fail", "--retry", "3", "--max-time", "1800",
            "-H", f"Authorization: Bearer {token}",
            "-o", str(out), item["archive_download_url"],
        ]
        proc = subprocess.run(cmd, capture_output=True, text=True)
        if proc.returncode == 0:
            print(f"  [OK ] {name}  {item.get('size_in_bytes', 0) // 1024} KiB  → {out.name}")
        else:
            print(f"  [FAIL] {name}: {proc.stderr.strip()[:200]}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("run_id", nargs="?", type=int, default=0)
    ap.add_argument("--repo", default=DEFAULT_REPO)
    ap.add_argument("--workflow", default=DEFAULT_WORKFLOW)
    ap.add_argument("--interval", type=int, default=45)
    ap.add_argument("--download", default="")
    ap.add_argument("--token", default="")
    ap.add_argument("--latest", action="store_true")
    args = ap.parse_args()

    token = find_token(args.token)
    if not token:
        print("[!] 没有凭据：public 仓库可以只看状态，但下载产物需要 token")

    run_id = args.run_id
    if args.latest or not run_id:
        run_id = latest_run(args.repo, args.workflow, token)
        print(f"[*] 最近一次 run: {run_id}")

    while True:
        status, conclusion = show(args.repo, run_id, token)
        if status == "completed":
            print(f"\n[=] 结论: {conclusion}")
            break
        time.sleep(args.interval)

    if args.download:
        download(args.repo, run_id, token, Path(args.download))
    else:
        for item in artifacts(args.repo, run_id, token):
            print(f"  - {item['name']}  {item.get('size_in_bytes', 0) // 1024} KiB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
