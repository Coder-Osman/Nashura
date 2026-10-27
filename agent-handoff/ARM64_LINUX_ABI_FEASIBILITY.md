# Apple Silicon → ARM64 Linux ABI 可行性实测记录

日期 2026-10-27；任务 E2.1-N；基线 `82b94be5ae5ceb52f8cee0f5873ec3b8845bb573`。

## 本轮结果

| 项目 | 状态 | 实测 |
|---|---|---|
| Darwin ARM64 环境识别 | IMPLEMENTED | macOS 15.6/24G84，36 GiB，初始约 162.9 GiB 空闲 |
| Fusion 运行实例 | BLOCKED | vmrun list exit 0，0 台运行；现有 VM 有挂起状态，保持原状 |
| guest 启动/网络/uname | NOT_RUN | 没有可用会话，不根据 VM 名称或配置推断 live 架构 |
| Linux syscall 静态探针 | IMPLEMENTED | AArch64 ELF、无 libc/DT_NEEDED/PT_INTERP；交叉构建 exit 0 |
| Android Bionic 探针 | IMPLEMENTED | AArch64 ELF；PT_INTERP=/system/bin/linker64，依赖 libdl.so/libc.so；构建 exit 0 |
| Darwin 上的执行守门器 | UNIT_PASS | 真实宿主调用 exit 2 / BLOCKED_ENVIRONMENT；未执行 ELF |
| guest direct smoke | BLOCKED | 没有运行记录，未授予 PURE_LINUX_SMOKE_PASS |
| Bionic linker/线程/TLS | NOT_RUN | 文件匹配不替代运行 |
| 真实 libg load / lifecycle | NOT_RUN | 无 LOAD_ONLY、NATIVE_VERIFIED 或原生一致性证据 |

macOS/Mach-O 与 Linux/Android ELF 属于不同 OS ABI；ARM64 同指令集不消除差异。
独立 ARM64 Linux guest 可直接执行 ARM64 指令，不需要 QEMU user-mode 翻译；
但仍需验证 Android linker、Bionic、namespace、JNI/资源和初始化对象图。
未在 macOS 尝试加载 libg，也未用 Linux ELF 的 Exec format error 否定原版逻辑可剥离性。

## 已实现的合成探针

`arm64_start.S` / `arm64_smoke.c`：原生 Linux syscall、pid/tid、monotonic clock、
文件创建/写入/seek/读取、Unix socketpair IPC、clone 线程、TPIDR_EL0 父子 TLS 隔离、
signalfd 真实信号及 JSONL 日志。单独 `--exit-23` 核对退出码传递。
这不是游戏程序，raw TLS 也不是 Bionic pthread TLS 证明。

`run_arm64_smoke.py`：拒绝 Darwin 和非 ARM64 Linux；核验 SHA/ELF 架构后才执行，
独立目录、每次 20 秒 timeout、逐项日志、文件 canary 与退出码同时通过才生成
PURE_LINUX_SMOKE_PASS；`native_game_verified` 始终 false。输出目录排他创建。

`bionic_probe.c`：Bionic clock、pthread、C TLS 探针。第一轮不传库路径。
显式传入库参数才尝试 dlopen；成功只记 LOAD_ONLY，constructors 可能已运行，
并不调用 JNI_OnLoad 或战斗生命周期。当前没有执行这条路径。

## 已执行构建命令

下列变量仅用于脱敏重放：NDK_BIN 为已有 NDK 27.3.13750724 工具目录，
EVIDENCE 为本轮排他私有目录，REPO 为此任务工作树；完整实际 argv 和日志留在私有记录。

```sh
"$NDK_BIN/clang" --target=aarch64-linux-gnu -fuse-ld=lld \
  -nostdlib -static -ffreestanding -fno-builtin -fno-stack-protector \
  -Wall -Wextra -Werror -O2 tools/pure_linux/arm64_start.S \
  tools/pure_linux/arm64_smoke.c -o "$EVIDENCE/arm64-linux-smoke-v3"

"$NDK_BIN/aarch64-linux-android24-clang" -Wall -Wextra -Werror -O2 \
  tools/pure_linux/bionic_probe.c -ldl -o "$EVIDENCE/bionic-probe-v2"
```

两条 exit 0。输出 SHA：

- Linux smoke：`2249f3439c240dd6bde1aeb8652a6c1de98a04bb1fdf30f3a9f7054af9b6e8b1`
- Bionic probe：`02dbc40e7ff04eafd633d24b1c8bd69fe88959d9105c5cb8cfe4e78c7a8db9cc`

上述二进制只保存在私有目录，不提交。旧构建和失败输出也保留。

## 待 guest 可用后执行，以下并非本轮已运行命令

```sh
uname -s
uname -m
python3 tools/pure_linux/run_arm64_smoke.py \
  --elf "$EVIDENCE/arm64-linux-smoke-v3" \
  --sha256 2249f3439c240dd6bde1aeb8652a6c1de98a04bb1fdf30f3a9f7054af9b6e8b1 \
  --output "$EVIDENCE/new-guest-smoke"
```

先固定 guest 系统/内核、网络、镜像身份及实际日志。只有此项通过后才验证 Bionic。
匹配的 linker64 SHA 为 `e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7`；
214 个 sysroot manifest 文件已匹配。后续需在独立 guest 实验根中配置 Android 文件布局，
明确 namespace/依赖搜索策略；不在现有 VM 上覆盖 `/system` 或安装运行时。
构造器、属性区、TLS、版本符号及间接加载的首个失败应逐项记录，不预先铺开完整 framework。

旧 `start.S` / `smoke.c` / `run_smoke.py` 是 x86_64 Linux + QEMU 历史工具，原字节保留；
它们不充当本轮 ARM64 guest 证据。当前限制下的最小下一步是提供独立可用 guest 会话。
