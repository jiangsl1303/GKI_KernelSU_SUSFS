#!/usr/bin/env bash
# 把本地工作树同步成 fork 的 main（丢弃 fork 上那 7 个失效 pin 提交）。
#
# 用法：
#   GH_TOKEN=ghp_xxx bash tools/sync_push.sh            # 真推
#   bash tools/sync_push.sh --plan                      # 只看要做什么
#
# 回滚：推送前脚本会把旧的 fork 头记到 refs/backup/pre-sync-<时间>，
#       需要恢复时：git push --force-with-lease origin refs/backup/pre-sync-XXX:main
set -euo pipefail

REPO_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_DIR"

ORIGIN_URL="https://github.com/jiangsl1303/GKI_KernelSU_SUSFS.git"
UPSTREAM_URL="https://github.com/WildKernels/GKI_KernelSU_SUSFS.git"
BRANCH="${BRANCH:-main}"
PLAN=0
[ "${1:-}" = "--plan" ] && PLAN=1

say() { printf '%s\n' "$*"; }

say "[*] 工作目录: $REPO_DIR"
say "[*] 本地 HEAD : $(git rev-parse --short HEAD)  $(git log -1 --format=%s)"

if [ -z "${GH_TOKEN:-}" ] && [ -z "${GITHUB_TOKEN:-}" ]; then
  if [ -f "$REPO_DIR/../.secrets/gh_token" ]; then
    GH_TOKEN="$(cat "$REPO_DIR/../.secrets/gh_token")"
  elif [ -f "$REPO_DIR/.secrets/gh_token" ]; then
    GH_TOKEN="$(cat "$REPO_DIR/.secrets/gh_token")"
  fi
fi
if [ -z "${GH_TOKEN:-}" ]; then
  say "[!] 没有 GH_TOKEN（或 ../.secrets/gh_token）—— 只做本地检查"
  PLAN=1
fi

PUSH_URL="https://x-access-token:${GH_TOKEN:-none}@github.com/jiangsl1303/GKI_KernelSU_SUSFS.git"

say ""
say "== 1) 补齐历史（当前是浅克隆，推上去会把历史截断）=="
if [ -f .git/shallow ]; then
  say "    shallow → unshallow（从 upstream 拉全量历史）"
  [ "$PLAN" = "1" ] || git fetch --unshallow "$UPSTREAM_URL" "+refs/heads/*:refs/remotes/upstream/*" || \
    say "    [!] unshallow 失败（网络？）—— 仍可强制推，但 fork 上历史会变浅"
else
  say "    已是完整历史"
fi

say ""
say "== 2) 记录 fork 当前头（回滚用）=="
OLD_FORK_SHA="$(git ls-remote "$ORIGIN_URL" "refs/heads/$BRANCH" | cut -f1 || true)"
say "    fork/$BRANCH = ${OLD_FORK_SHA:-<空>}"
if [ -n "${OLD_FORK_SHA:-}" ] && [ "$PLAN" = "0" ]; then
  git update-ref "refs/backup/pre-sync-$(date +%Y%m%d-%H%M%S)" "$OLD_FORK_SHA" || true
fi

say ""
say "== 3) 校验上游是否又前进 =="
UP_SHA="$(git ls-remote "$UPSTREAM_URL" "refs/heads/$BRANCH" | cut -f1 || true)"
say "    upstream/$BRANCH = ${UP_SHA:-<取不到>}"
if [ -n "${UP_SHA:-}" ]; then
  if git cat-file -e "$UP_SHA^{commit}" 2>/dev/null; then
    if git merge-base --is-ancestor "$UP_SHA" HEAD; then
      say "    上游 HEAD 已包含在本地 → 我们只多 $(git rev-list --count "$UP_SHA"..HEAD) 个提交（理想状态）"
    else
      say "    [!] 上游已前进：我们缺少 $(git rev-list --count HEAD.."$UP_SHA") 个上游提交"
      say "        先 git fetch upstream && git rebase upstream/$BRANCH 再推，避免把上游修复盖掉"
    fi
  else
    say "    本地没有该提交对象（浅克隆）→ unshallow 后再确认"
  fi
fi

say ""
say "== 4) 推送 =="
LEASH=""
if [ -n "${OLD_FORK_SHA:-}" ]; then
  LEASH="--force-with-lease=refs/heads/$BRANCH:$OLD_FORK_SHA"
fi
say "    git push $LEASH origin HEAD:$BRANCH"
if [ "$PLAN" = "1" ]; then
  say "[*] --plan / 无凭据 → 不推。要真推请：GH_TOKEN=... bash tools/sync_push.sh"
  exit 0
fi
git push $LEASH "$PUSH_URL" "HEAD:refs/heads/$BRANCH"
say "[+] 已推送。接下来："
say "    python tools/trigger.py --families android14-6.1 --os-patch-level lts"
