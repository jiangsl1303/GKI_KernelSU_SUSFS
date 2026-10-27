#!/usr/bin/env python3
"""推送/触发之前的本地预检（不连 GitHub Actions，就能挡掉大部分必然失败）。

检查项：
  1. 所有 workflow / action 的 YAML 语法 + 上游自带的 validate_workflows.py
  2. 本仓库新增 workflow 里每个 `run:` shell 块的语法（bash -n）
  3. 跨 workflow 的输入契约：gki-latest.yml → prepare.yml → build.yml 的 `with:` 键
     必须都在被调用方的 workflow_call.inputs 里（拼错一个键 = 线上直接失败）
  4. 版本可解析：最新 KernelSU-Next(dev-susfs) + 各 KMI 的 SUSFS tip 与版本号
  5. 本地树相对上游 main 的位置（是否落后 / 有未提交改动）

用法：
  python tools/preflight.py                # 全查
  python tools/preflight.py --no-network   # 跳过联网解析
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
WF = ROOT / ".github" / "workflows"
TOOLS = ROOT / "tools"

OK, WARN, FAIL = "[ OK ]", "[WARN]", "[FAIL]"
failures: list[str] = []
warnings: list[str] = []


def ok(msg: str) -> None:
    print(f"{OK} {msg}")


def warn(msg: str) -> None:
    warnings.append(msg)
    print(f"{WARN} {msg}")


def fail(msg: str) -> None:
    failures.append(msg)
    print(f"{FAIL} {msg}")


def load_yaml(path: Path):
    return yaml.safe_load(path.read_text(encoding="utf-8"))


def bash_available() -> bool:
    return shutil.which("bash") is not None


def check_yaml() -> None:
    print("\n== 1. YAML 语法 ==")
    files = sorted(WF.glob("*.yml")) + sorted(WF.glob("*.yaml"))
    files += sorted((ROOT / ".github" / "actions").rglob("action.yml"))
    for path in files:
        try:
            load_yaml(path)
        except yaml.YAMLError as exc:
            fail(f"{path.relative_to(ROOT)}: YAML 解析失败 {exc}")
    ok(f"{len(files)} 个 YAML 文件解析通过")


def run_upstream_validators() -> None:
    print("\n== 2. 上游自带校验脚本 ==")
    for script in ("validate_workflows.py", "validate_shell.py", "validate_python.py"):
        path = ROOT / ".github" / "scripts" / script
        if not path.exists():
            warn(f"缺少 {script}")
            continue
        proc = subprocess.run(
            [sys.executable, str(path)], cwd=ROOT, capture_output=True, text=True
        )
        if proc.returncode == 0:
            ok(f"{script} 通过")
        else:
            fail(f"{script} 失败\n{proc.stdout[-1500:]}{proc.stderr[-1500:]}")


def iter_run_blocks(node, path="") -> list[tuple[str, str]]:
    """递归收集所有 shell 脚本块（run: 字符串）。"""
    found: list[tuple[str, str]] = []
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "run" and isinstance(value, str):
                found.append((path, value))
            else:
                found.extend(iter_run_blocks(value, f"{path}/{key}"))
    elif isinstance(node, list):
        for idx, item in enumerate(node):
            found.extend(iter_run_blocks(item, f"{path}[{idx}]"))
    return found


def check_shell_blocks() -> None:
    print("\n== 3. shell 块语法（bash -n）==")
    if not bash_available():
        warn("本机没有 bash，跳过 shell 语法检查")
        return
    broken = 0
    total = 0
    for path in sorted(WF.glob("*.yml")):
        data = load_yaml(path)
        for where, script in iter_run_blocks(data, path.name):
            total += 1
            with tempfile.NamedTemporaryFile(
                "w", suffix=".sh", delete=False, encoding="utf-8", newline="\n"
            ) as fh:
                fh.write(script + "\n")
                tmp = fh.name
            proc = subprocess.run(["bash", "-n", tmp], capture_output=True, text=True)
            Path(tmp).unlink(missing_ok=True)
            if proc.returncode != 0:
                broken += 1
                fail(f"{where}: bash -n 失败\n{proc.stderr.strip()[:600]}")
    if not broken:
        ok(f"{total} 个 shell 块语法通过")


def workflow_inputs(path: Path) -> set[str]:
    data = load_yaml(path)
    # PyYAML 会把裸 on: 解析成 True
    on = data.get("on", data.get(True)) or {}
    call = on.get("workflow_call") if isinstance(on, dict) else None
    if not call:
        return set()
    return set((call.get("inputs") or {}).keys())


def collect_with_keys(job: dict) -> dict[str, str]:
    return {k: str(v) for k, v in (job.get("with") or {}).items()}


def check_call_contracts() -> None:
    print("\n== 4. 跨 workflow 输入契约 ==")
    latest = load_yaml(WF / "gki-latest.yml")
    prepare = load_yaml(WF / "prepare.yml")
    build = load_yaml(WF / "build.yml")

    prepare_inputs = workflow_inputs(WF / "prepare.yml")
    build_inputs = workflow_inputs(WF / "build.yml")

    build_job = latest["jobs"].get("build")
    if not build_job:
        fail("gki-latest.yml 缺少 build job")
        return
    for key in collect_with_keys(build_job):
        if key not in prepare_inputs:
            fail(f"gki-latest.yml → prepare.yml: 未声明的输入 '{key}'")
    ok(f"gki-latest.yml → prepare.yml: {len(collect_with_keys(build_job))} 个输入全部存在")

    inner = (prepare["jobs"] or {}).get("build") or {}
    for key in collect_with_keys(inner):
        if key not in build_inputs:
            fail(f"prepare.yml → build.yml: 未声明的输入 '{key}'")
    ok(f"prepare.yml → build.yml: {len(collect_with_keys(inner))} 个输入全部存在")

    # root_flavor=next 时 build.yml 内部会再解析 SUSFS tip，确认这条分支还在
    body = (WF / "build.yml").read_text(encoding="utf-8")
    if 'root_flavor }}" = "next"' in body and "refs/heads/${BRANCH}" in body:
        ok("build.yml 仍会在 root_flavor=next 时取 SUSFS 分支 tip")
    else:
        warn("build.yml 的「next 取 SUSFS tip」逻辑看起来变了，请人工确认版本策略")


def check_configs() -> None:
    print("\n== 5. 家族配置 ==")
    configs = sorted((ROOT / ".github" / "config").glob("*.json"))
    if not configs:
        fail(".github/config 下没有家族配置")
        return
    for path in configs:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            fail(f"{path.name}: JSON 解析失败 {exc}")
            continue
        if not data.get("include"):
            fail(f"{path.name}: include 为空")
    ok(f"{len(configs)} 个家族配置可解析")


def check_resolution() -> None:
    print("\n== 6. 最新版本解析（联网）==")
    proc = subprocess.run(
        [sys.executable, str(TOOLS / "resolve_latest.py"), "--families", "all", "--os-patch-level", "lts"],
        cwd=ROOT,
        capture_output=True,
        text=True,
    )
    if proc.returncode != 0:
        fail(f"resolve_latest.py 失败\n{proc.stdout[-1200:]}{proc.stderr[-2000:]}")
        return
    print(proc.stdout.rstrip())
    out = proc.stdout
    m = re.search(r"KernelSU-Next\s*:\s*([0-9a-f]{40})", out)
    if not m:
        fail("解析结果里没有 KernelSU-Next commit")
        return
    rows = re.findall(r"^\s+- (\S+)\s+(v[0-9][0-9.]*)\s+([0-9a-f]{40})$", out, re.MULTILINE)
    if not rows:
        fail("解析结果里没有 SUSFS 版本号")
    else:
        versions = sorted({ver for _, ver, _ in rows})
        ok(f"SUSFS 版本: {', '.join(versions)}（{len(rows)} 个 KMI 家族）")


def check_git_state() -> None:
    print("\n== 7. Git 状态 ==")
    if not (ROOT / ".git").exists():
        warn("不是 git 仓库，跳过")
        return
    try:
        head = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        ok(f"本地 HEAD = {head}")
        dirty = subprocess.run(
            ["git", "status", "--porcelain"], cwd=ROOT, capture_output=True, text=True, check=True
        ).stdout.strip()
        if dirty:
            warn(f"有未提交改动 {len(dirty.splitlines())} 项 —— 触发构建前记得提交（Actions 只跑已推送代码）")
        else:
            ok("工作区干净")
    except (subprocess.CalledProcessError, OSError) as exc:
        warn(f"git 查询失败: {exc}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--no-network", action="store_true", help="跳过联网版本解析")
    args = ap.parse_args()

    print("GKI Latest 预检 — 仓库:", ROOT)
    check_yaml()
    run_upstream_validators()
    check_shell_blocks()
    check_call_contracts()
    check_configs()
    if not args.no_network:
        check_resolution()
    check_git_state()

    print("\n== 结论 ==")
    if failures:
        print(f"{FAIL} {len(failures)} 项失败：")
        for item in failures:
            print("   -", item.splitlines()[0])
        return 1
    if warnings:
        print(f"{WARN} {len(warnings)} 项警告（不阻塞）")
    print(f"{OK} 预检通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
