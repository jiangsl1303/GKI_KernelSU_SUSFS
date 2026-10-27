#!/usr/bin/env python3
"""解析「最新 KernelSU-Next + 最新 SUSFS」的当前 commit，并生成构建矩阵。

用途：
  1) 本地预检：`python tools/resolve_latest.py`                     —— 打印当前会构建什么
  2) CI 使用 ：在 resolve job 里带 `--github-output $GITHUB_OUTPUT` —— 写 workflow outputs

设计要点（与上游 WildKernels/GKI_KernelSU_SUSFS 的事实对齐，勿随意改）：
  * GKI 的 KernelSU-Next 内核侧必须带 SUSFS 集成 —— 官方 KernelSU-Next 不带，
    上游统一用 pershoot/KernelSU-Next 的 `dev-susfs` 分支（= 官方 dev + SUSFS 集成）。
    所以 "最新 KernelSU-Next" = `refs/heads/dev-susfs` 的当前 tip。
  * SUSFS 必须与内核 KMI 分支一一对应：simonpunk/susfs4ksu 的 `gki-<android><kver>`。
    KernelSU-Next 场景下**必须取分支 tip**（build.yml 内部也会再取一次 tip），
    任何「钉死旧 SUSFS + 新内核」的组合都会出现大量 .rej。
  * 其它被 build.yml 需要的 commit（kernel_patches / AnyKernel3 / NoMount / DroidSpaces）
    也在这里一并解析，避免 workflow 里出现第二份真源。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_DIR = REPO_ROOT / ".github" / "config"

KSUN_REPO = "https://github.com/pershoot/KernelSU-Next.git"
KSUN_BRANCH = "refs/heads/dev-susfs"
KSUN_FALLBACK_REPO = "https://github.com/KernelSU-Next/KernelSU-Next.git"
KSUN_FALLBACK_BRANCH = "refs/heads/dev"

SUSFS_REPO = "https://gitlab.com/simonpunk/susfs4ksu.git"
SUSFS_RAW = "https://gitlab.com/simonpunk/susfs4ksu/-/raw/{branch}/kernel_patches/include/linux/susfs.h"

HELPERS = {
    # key -> (repo, ref)
    "nomount_commit": ("https://github.com/maxsteeel/nomount.git", "refs/heads/dev"),
    "kernel_patches_commit": ("https://github.com/WildKernels/kernel_patches.git", "refs/heads/main"),
    "anykernel3_commit": ("https://github.com/WildKernels/AnyKernel3.git", "refs/heads/gki-2.0"),
    "droidspaces_commit": ("https://github.com/ravindu644/Droidspaces-OSS.git", "refs/heads/main"),
}

VALID_FEATURES = [
    "SUSFS", "NoMount", "BBG", "NET", "DS", "NTSync", "Ptrace", "Unicode", "BPF", "Perf",
]

SHA_RE = re.compile(r"^[0-9a-f]{40}$")


def run(cmd: list[str], retries: int = 3, timeout: int = 90) -> str:
    last = ""
    for attempt in range(1, retries + 1):
        try:
            out = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout, check=True
            )
            return out.stdout.strip()
        except subprocess.CalledProcessError as exc:
            last = (exc.stderr or exc.stdout or "").strip()
        except (subprocess.TimeoutExpired, OSError) as exc:
            last = str(exc)
        if attempt < retries:
            time.sleep(3 * attempt)
    raise RuntimeError(f"command failed after {retries} tries: {' '.join(cmd)}\n{last}")


def resolve_sha(repo: str, ref: str) -> str:
    """解析分支 tip：先 git ls-remote，失败再走宿主 API（本机代理常把 git 打挂 502）。"""
    branch = ref.replace("refs/heads/", "", 1)
    try:
        out = run(["git", "ls-remote", repo, ref])
        for line in out.splitlines():
            sha, _, name = line.partition("\t")
            if name.strip() == ref:
                return sha.strip()
    except RuntimeError as exc:
        print(f"[!] git ls-remote 失败，改用 API 兜底：{repo} {ref}\n    {str(exc).splitlines()[-1]}", file=sys.stderr)

    if "github.com" in repo:
        slug = repo.rstrip("/").split("github.com/")[-1].removesuffix(".git")
        data = json.loads(fetch_text(f"https://api.github.com/repos/{slug}/git/ref/heads/{branch}"))
        commit = (data.get("object") or {}).get("sha", "")
        if not commit:
            # 注解 tag 的情况
            commit = ((data.get("object") or {}).get("object") or {}).get("sha", "")
        return commit
    if "gitlab.com" in repo:
        slug = repo.rstrip("/").split("gitlab.com/")[-1].removesuffix(".git")
        project = urllib.parse.quote(slug, safe="")
        url = f"https://gitlab.com/api/v4/projects/{project}/repository/branches/{urllib.parse.quote(branch, safe='')}"
        data = json.loads(fetch_text(url))
        return (data.get("commit") or {}).get("id", "")
    return ""


def fetch_text(url: str, retries: int = 4) -> str:
    last = ""
    for attempt in range(1, retries + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "curl/8"})
            token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
            if token and "api.github.com" in url:
                req.add_header("Authorization", f"Bearer {token}")
            with urllib.request.urlopen(req, timeout=45) as resp:
                return resp.read().decode("utf-8", "replace")
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = str(exc)
            if attempt < retries:
                time.sleep(2 * attempt)
    raise RuntimeError(f"fetch failed: {url}\n{last}")


def susfs_version(family: str) -> str:
    text = fetch_text(SUSFS_RAW.format(branch=f"gki-{family}"))
    m = re.search(r'#define\s+SUSFS_VERSION\s+"([^"]+)"', text)
    return m.group(1) if m else ""


def load_families(requested: str) -> tuple[list[str], bool]:
    """返回 (家族列表, 是否显式指定)。"""
    available = sorted(p.stem for p in CONFIG_DIR.glob("*.json"))
    if requested.strip().lower() in ("", "all"):
        return available, False
    wanted = [f.strip() for f in requested.split(",") if f.strip()]
    unknown = [f for f in wanted if f not in available]
    if unknown:
        raise SystemExit(
            f"[!] 未知 GKI 家族: {', '.join(unknown)}\n    可选: {', '.join(available)}"
        )
    return wanted, True


def matches(entry: dict, selector: str) -> bool:
    """与 prepare.yml 的过滤规则保持一致。"""
    sel = selector.strip().lower()
    if sel in ("", "all", "*"):
        return True
    if str(entry.get("date", "")).lower() == sel:
        return True
    return str(entry.get("sublevel", "")) == sel


def parse_feature_set(raw: str) -> str:
    tokens = [t.strip() for t in raw.split(",") if t.strip()]
    if not tokens:
        tokens = ["SUSFS"]
    bad = [t for t in tokens if t not in VALID_FEATURES]
    if bad:
        raise SystemExit(
            f"[!] 未知特性: {', '.join(bad)}\n    可选: {', '.join(VALID_FEATURES)}"
        )
    if "SUSFS" not in tokens:
        raise SystemExit("[!] KernelSU-Next 内核必须带 SUSFS（本工作流目标），请在 features 里保留 SUSFS")
    return "+".join(tokens)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", default="all", help="all 或逗号分隔的 GKI 家族")
    ap.add_argument("--os-patch-level", default="lts", help="lts / all / YYYY-MM / sublevel")
    ap.add_argument("--features", default="SUSFS", help="逗号分隔特性集")
    ap.add_argument("--json", default="", help="把结果写入该 JSON 文件")
    ap.add_argument("--github-output", default="", help="写入 GITHUB_OUTPUT 的路径")
    ap.add_argument("--quiet", action="store_true")
    args = ap.parse_args()

    say = (lambda *a: None) if args.quiet else (lambda *a: print(*a, file=sys.stderr))

    families, explicit = load_families(args.families)
    feature_set = parse_feature_set(args.features)

    say("[*] 解析 KernelSU-Next（SUSFS 集成分支）…")
    ksu_commit = ""
    ksu_ref = f"pershoot@dev-susfs"
    try:
        ksu_commit = resolve_sha(KSUN_REPO, KSUN_BRANCH)
    except RuntimeError as exc:
        say(f"[!] pershoot dev-susfs 解析失败，回退官方 dev：{exc}")
    if not SHA_RE.match(ksu_commit or ""):
        try:
            ksu_commit = resolve_sha(KSUN_FALLBACK_REPO, KSUN_FALLBACK_BRANCH)
        except RuntimeError as exc:
            raise SystemExit(f"[!] 官方 dev 兜底也失败（网络问题？）\n{exc}") from exc
        ksu_ref = "official@dev (无 SUSFS 集成，仅兜底)"
    if not SHA_RE.match(ksu_commit or ""):
        raise SystemExit("[!] 无法解析 KernelSU-Next commit")

    matrix: list[dict] = []
    susfs_info: dict[str, dict] = {}
    for family in families:
        cfg_path = CONFIG_DIR / f"{family}.json"
        cfg = json.loads(cfg_path.read_text(encoding="utf-8"))
        targets = [e for e in cfg.get("include", []) if matches(e, args.os_patch_level)]
        if not targets:
            say(f"[-] {family}: 没有匹配 os_patch_level='{args.os_patch_level}' 的目标，跳过")
            continue

        branch = f"gki-{family}"
        try:
            sha = resolve_sha(SUSFS_REPO, f"refs/heads/{branch}")
        except RuntimeError as exc:
            if explicit:
                raise SystemExit(f"[!] {family}: 解析 SUSFS 分支 {branch} 失败\n{exc}")
            say(f"[-] {family}: 解析 SUSFS 分支失败，跳过\n{exc}")
            continue
        if not SHA_RE.match(sha or ""):
            # simonpunk 目前没有 gki-android17-6.18 分支：该 KMI 无 SUSFS 补丁可用。
            msg = f"{family}: simonpunk/susfs4ksu 没有 {branch} 分支（该 KMI 暂无 SUSFS 补丁）"
            if explicit:
                raise SystemExit(f"[!] {msg}")
            say(f"[-] {msg}，跳过")
            continue
        ver = susfs_version(family)
        if not ver:
            raise SystemExit(f"[!] {family}: 取不到 susfs.h 的 SUSFS_VERSION")
        susfs_info[family] = {
            "branch": branch,
            "commit": sha,
            "version": ver,
            "targets": [f"{e.get('sublevel')}@{e.get('date')}" for e in targets],
        }
        matrix.append(
            {
                "family": family,
                "config_file": f".github/config/{family}.json",
                "susfs_commit": sha,
                "susfs_version": ver,
            }
        )
        say(f"[+] {family}: SUSFS {ver} @ {sha[:12]}  目标 {', '.join(susfs_info[family]['targets'])}")

    if not matrix:
        raise SystemExit("[!] 过滤后没有任何可构建目标")

    helpers: dict[str, str] = {}
    for key, (repo, ref) in HELPERS.items():
        helpers[key] = resolve_sha(repo, ref)
        if not SHA_RE.match(helpers[key] or ""):
            raise SystemExit(f"[!] 解析 {key} 失败: {repo} {ref}")

    result = {
        "ksu_commit": ksu_commit,
        "ksu_ref": ksu_ref,
        "feature_set": feature_set,
        "os_patch_level": args.os_patch_level,
        "matrix": {"include": matrix},
        "susfs": susfs_info,
        **helpers,
    }

    if args.json:
        Path(args.json).write_text(
            json.dumps(result, indent=2, ensure_ascii=False), encoding="utf-8"
        )

    if args.github_output:
        with open(args.github_output, "a", encoding="utf-8") as fh:
            fh.write(f"ksu_commit={ksu_commit}\n")
            fh.write(f"ksu_ref={ksu_ref}\n")
            fh.write(f"feature_set={feature_set}\n")
            fh.write("matrix=" + json.dumps(result["matrix"], separators=(",", ":")) + "\n")
            fh.write("susfs_table=" + json.dumps(susfs_info, separators=(",", ":")) + "\n")
            for key, value in helpers.items():
                fh.write(f"{key}={value}\n")

    if not args.quiet:
        print("=" * 78)
        print(f"KernelSU-Next : {ksu_commit}  ({ksu_ref})")
        print(f"SUSFS         : 见下表（simonpunk/susfs4ksu 各 KMI 分支 tip）")
        for family, info in susfs_info.items():
            print(f"  - {family:<16} {info['version']:<8} {info['commit']}")
        print(f"feature_set   : {feature_set}")
        print(f"os_patch_level: {args.os_patch_level}")
        print(f"矩阵条目      : {len(matrix)}")
        print(f"kernel_patches: {helpers['kernel_patches_commit']}")
        print(f"AnyKernel3    : {helpers['anykernel3_commit']}")
        print("=" * 78)
    return 0


if __name__ == "__main__":
    sys.exit(main())
