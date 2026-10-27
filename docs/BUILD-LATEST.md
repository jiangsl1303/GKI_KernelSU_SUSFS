# 构建「最新 KernelSU-Next + 最新 SUSFS」的 GKI 内核

本仓库是 [WildKernels/GKI_KernelSU_SUSFS](https://github.com/WildKernels/GKI_KernelSU_SUSFS)
的 fork，**只做加法**：上游 5000 多行的工作流/action 一动不改，新增一条
`GKI Latest (KernelSU-Next + SUSFS)` 工作流 + `tools/` 下的预检/触发/监控脚本。

---

## 1. 三个名字先说清楚

| 说法 | 实际指什么 | 为什么 |
|---|---|---|
| **GKI** | Google 的 `kernel/common`（`repo init -u .../kernel/manifest -b common-androidXX-<kver>-<patch>`） | 通用内核镜像，能刷任何同 KMI 的 GKI 2.0 设备 |
| **最新 SUSFS** | `gitlab.com/simonpunk/susfs4ksu` 各 KMI 分支 **tip**（当前 v2.3.0） | SUSFS 的 fs 补丁和内核树强相关，钉死旧版 = 大量 `.rej` |
| **最新 KernelSU-Next** | `github.com/pershoot/KernelSU-Next` 的 `dev-susfs` 分支 tip（当前 d21e0a66，VERSION 33405） | **官方** KernelSU-Next 不带 SUSFS 集成，硬套 `10_enable_susfs_for_ksu.patch` 会链接失败（`fake_state`/`selinux_hide` 符号缺失）。pershoot 的 `dev-susfs` = 官方 dev + SUSFS 集成提交，官方 `dev` 与它同一基线 |

管理器（APK）仍然用**官方** KernelSU-Next dev 构建，不用 fork —— 内核侧才需要 fork。

---

## 2. 一键触发

### 网页
`Actions` → `GKI Latest (KernelSU-Next + SUSFS)` → `Run workflow`

| 输入 | 说明 | 推荐 |
|---|---|---|
| `families` | `all` 或逗号分隔，如 `android14-6.1,android15-6.6` | 先单家族验证 |
| `os_patch_level` | `lts`（只构建各分支的 LTS 头）/ `all`（配置里的全部月份）/ `2026-09` / `141` | `lts` |
| `features` | 逗号分隔，默认只有 `SUSFS` | `SUSFS` |
| `page_size` | `4k` / `16k`（16k 仅 6.1/6.6/6.12） | `4k` |
| `use_cache` | fork 里没有 ccache/bazel 缓存桶，**默认关** | 关 |

### 命令行（本机）

```bash
python tools/preflight.py                     # 先本地预检（不联网构建，挡掉必然失败）
python tools/trigger.py --families android14-6.1 --os-patch-level lts
python tools/monitor.py --latest --download outputs
```

`trigger.py` 的凭据顺序：`--token` → `GH_TOKEN`/`GITHUB_TOKEN` → 工作区 `.secrets/gh_token`。
没有凭据时它只打印要填的选项，不报错。

---

## 3. 产物

每次 build 产出三个 artifact（按 `sublevel-android-<kver>-<patch>-KernelSU-Next` 命名）：

| Artifact | 内容 |
|---|---|
| `*-AnyKernel3` | 刷机包（AnyKernel3 zip，含 Image/dtb，直接 KernelFlasher/Recovery 刷） |
| `*-BuildInfo` | 构建摘要：内核版本串、KSU 版本、SUSFS 版本与 commit、来源 run |
| `*-Rejects` | 补丁 `.rej`（**出现非空就是有补丁没打上**，必须看） |

刷机前先看 `*-BuildInfo` 里 `SUSFS Version` 与 `KSU Version` 是否等于 `resolve` 作业打印的值。

---

## 4. 这套流程为什么能跑通（踩过的坑）

1. **版本必须成套**：KSUN(dev-susfs tip) + SUSFS(同 KMI 分支 tip) 是唯一被上游每晚验证的组合。
   任何「旧 SUSFS + 新内核」都会 `.rej` 爆炸；`9cead8ea`、`222173a6` 等上游提交就是在修这些补丁上下文。
2. **官方 KernelSU-Next + SUSFS 直接拼是不行的**：`kernel_patches/next/susfs_fix_patches/`
   只有到 v2.2.0 的 fix 补丁，SUSFS v2.3.0 没有对应 fix 集；走 pershoot 的 `dev-susfs`
   等于让 KernelSU 侧自己带集成，绕开 fix 补丁地狱。
3. **不要钉 r21 那套旧 pin**：2026-10-13 的 fork 曾把 KSUN/SUSFS 钉到 r21（`4c58531` / `5192aa2d`），
   结果内核是"LKM + built-in 半成品混合"，能编过但刷机 bootloop。上游已改为「next 永远跟 tip」。
4. **`feature_set` 越少越稳**：BBG / NoMount / DroidSpaces / NTSync / BPF 各自带一套补丁，
   任何一条与最新内核不匹配都会拖垮整个 job。默认只开 SUSFS，需要什么再逐个加。
5. **stock-configs 是硬性前置**：`prepare.yml` 里抓不到官方 GKI 预编译 config 会 **失败退出**
   （不是 best-effort）。这条依赖 `android.googlesource.com/kernel/prebuilts/<kver>/arm64`，
   偶发 5xx 时重跑即可。
6. **`android17-6.18` 目前无 SUSFS 分支**：simonpunk 还没有 `gki-android17-6.18`，
   所以 `families=all` 会自动跳过它（显式指定才报错）。

---

## 5. 排错对照表

| 现象 | 原因 | 处理 |
|---|---|---|
| resolve 作业失败：`Failed to resolve xxx` | 上游分支改名 / gitlab 抖动 | 看 `tools/resolve_latest.py` 输出，单独 `git ls-remote` 复现 |
| `no build targets match patch level` | `os_patch_level` 与 `.github/config/*.json` 里的 date/sublevel 不匹配 | 用 `lts` 或 `all`，或 `python tools/resolve_latest.py` 看可用目标 |
| 补丁步骤 exit 1 / `.rej` 一堆 | 内核 sublevel 与 SUSFS tip 不匹配 | 换 `lts`；或降到该分支更早的 sublevel |
| 链接错误 `undefined symbol: fake_state` | root 用成了不带 SUSFS 集成的 KernelSU | 确认 `root_flavor=next` 且 `root_commit` = pershoot `dev-susfs` |
| 刷入后 bootloop | KMI 不匹配 / 16k 设备刷了 4k 镜像 | 核对设备 `uname -r` 的 KMI 与页大小 |
| `Validate` 变红 | 新增工作流不符合上游校验规则 | `python .github/scripts/validate_workflows.py` 本地先跑 |

---

## 6. 本地目录

```
gki-next-susfs/
├── .github/workflows/gki-latest.yml   # 新增：一键最新 KSUN+SUSFS
├── .github/workflows/prepare.yml      # 上游：单家族矩阵 + stock configs
├── .github/workflows/build.yml        # 上游：真正编译 + 打包
├── .github/workflows/main.yml         # 上游：全功能驱动（多 root / 多特性）
├── tools/resolve_latest.py            # 解析最新 KSUN / SUSFS，产出构建矩阵
├── tools/preflight.py                 # 推送前本地预检（YAML/shell/输入契约/版本）
├── tools/trigger.py                   # workflow_dispatch 触发
├── tools/monitor.py                   # run 级监控 + 产物下载
└── tools/notify.py                    # Server酱推送
```
