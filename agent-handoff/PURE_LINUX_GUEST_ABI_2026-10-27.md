# E2.1-N 续验：真实 ARM64 Linux guest 与 Bionic ABI

日期：2026-10-27。分支 `codex/pure-linux-libg-headless-20261026`；父提交
`3326d9f9dc7838c37750dfc02fc7a16f38661e87`。本报告的发布提交以 Git 历史为准。
旧 BLOCKED_ENVIRONMENT 和失败记录保留，本文件记录获得用户恢复 VM 授权后的新实验。

## 实际结论

现有 Ubuntu VM 已恢复，无需下载新 VM。真实 guest 的 Linux 合成探针取得
**PURE_LINUX_SMOKE_PASS**；匹配 Android linker/Bionic 的合成线程/TLS 探针也退出 0。
没有运行原版 libg 的加载、初始化或战斗生命周期，未取得 LOAD_ONLY。
原版引擎的 no-VM、Android-free、原生一致性和 10× 仍为 NOT_RUN。

| 子项 | 状态 | 实际结果 |
|---|---|---|
| 静态 loader 审计工具及测试 | IMPLEMENTED / UNIT_PASS | 专项 29 项通过，其中新增 14 项 |
| 原生 Linux 合成 ABI | NATIVE_VERIFIED，仅合成程序 | 八项检查、文件 canary、退出码通过 |
| Android linker/Bionic 合成 ABI | NATIVE_VERIFIED，仅合成程序 | clock、pthread create/join、C TLS 隔离通过 |
| 真实 libg 加载 | MISSING_INPUT / NOT_RUN | 锁定 APK 身份缺口仍在；未触发 constructors |
| 原生 GameState 生命周期 | BLOCKED / NOT_RUN | 尚未验证内容上下文及 manager 的独立构造 |
| 正式训练、放量、性能目标 | NOT_RUN | 本轮未启动 |

此处 NATIVE_VERIFIED 的对象只是两个合成 ABI 程序，绝非原版游戏。

## 环境与隔离

- 宿主：Darwin arm64，macOS 15.6 / 24G84，36 GiB；约 159 GiB 可用磁盘。
- guest：Ubuntu 22.04.5 LTS，Linux 6.8.0-138-generic，实测 `uname -m=aarch64`。
- guest 内存 6,195,167,232 bytes，可用 5,521,088,512 bytes，无 swap；根盘余量
  2,293,212 KiB。只运行小型探针，未开展大量输入复制或容量测试。
- 使用现有 Fusion guest operations，未安装 SSH、系统组件或新 VM，未修改 VM 配置。
- 只写本实验新建的 `/tmp` 隔离目录；失败目录和 retry-v4 分开保留。
- guest 对 github.com 的 HTTPS 探测 exit 1，TLS 握手超时。离线探针运行不依赖网络。
- 只读进程清单中未见训练/游戏任务；未启动 Android Emulator，未操作其他任务进程。
- 没有创建 `/system`、chroot、全局动态库路径或伪 JavaVM/ready/config shim。

下面 `$GUEST`、`$EVIDENCE`、`$BASELINE`、`$NDK_BIN` 为脱敏路径变量；实际 argv、
原始 stdout/stderr 留在私有实验归档。公开 JSON 保存原始日志 SHA，不含账户、IP 或 VM 路径。

## Linux smoke：失败、定位与实际重验

首次 v3 程序在 process、clock、file、IPC 后进入 clone/thread 时 SIGSEGV：程序退出
`-11`，runner 退出 `1`；单独退出码探针仍返回 `23`。这次失败属于自有测试程序，
不是 libg 的失败。原始报告及输出保留，未改写为成功。

修复 `arm64_start.S`：AArch64 raw clone 参数为
`flags, stack, parent_tid, tls, child_tid`，与 x86_64 最后两个参数位置不同。
保留 x3 的 TLS 参数，x4 置零作为未使用的 child_tid。依据为
[Linux clone(2)](https://man7.org/linux/man-pages/man2/clone.2.html)，其下载原文的 SHA
保存在证据 JSON。新编译 v4 的命令：

```sh
"$NDK_BIN/clang" --target=aarch64-linux-gnu -fuse-ld=lld \
  -nostdlib -static -ffreestanding -fno-builtin -fno-stack-protector \
  -Wall -Wextra -Werror -O2 tools/pure_linux/arm64_start.S \
  tools/pure_linux/arm64_smoke.c -o "$EVIDENCE/arm64-linux-smoke-v4"
```

构建 exit 0；v4 SHA `1969edf15f4c7f7b34713675e0b30caf65fdcdac33bedea7c737fd6a1d99b232`。
复制到新 guest 子目录后实际执行：

```sh
python3 "$GUEST/run_arm64_smoke.py" \
  --elf "$GUEST/arm64-linux-smoke-v4" \
  --sha256 1969edf15f4c7f7b34713675e0b30caf65fdcdac33bedea7c737fd6a1d99b232 \
  --output "$GUEST/direct-smoke-output"
```

runner exit 0；正常程序 exit 0，`--exit-23` 实际 exit 23。八项检查为 process_syscall、
monotonic_clock、file_io、unix_socket_ipc、clone_thread_shared_state、tls_isolation、
signal_delivery、all_checks，全部通过；文件 canary 也通过。该 raw TLS 测试与 Bionic TLS 分开。

## Bionic：真实加载路径已核对

只复制匹配的 linker64、libc.so、libdl.so 以及自有探针至隔离目录，实际执行：

```sh
LD_LIBRARY_PATH="$GUEST" "$GUEST/linker64" --help
LD_LIBRARY_PATH="$GUEST" "$GUEST/linker64" "$GUEST/bionic-probe-v2"
LD_LIBRARY_PATH="$GUEST" "$GUEST/linker64" --list "$GUEST/bionic-probe-v2"
```

三项 exit 0。探针输出 `BIONIC_SYNTHETIC_ABI_ONLY`，stderr 为空；没有传入 dlopen 库参数。
`--list` 实际将 libc.so/libdl.so 解析到上述隔离目录，并列出 kernel vdso。
libc/libdl 声明的 ld-android.so 未在该列表单独给出文件绑定行；不推断其独立文件绑定。
这只证明本探针所需的 loader/Bionic 路径，未证明 libg 的完整 namespace 或依赖已解析。

| 输入 | SHA-256 |
|---|---|
| linker64 | e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7 |
| libc.so | 9fe640090067ed1cb993815eb5d99fc03d80076754deafa813d0fd78dbb40569 |
| libdl.so | 6ac8782db7bbb2d28acf225a2b78aeb117ac64148a36724dedf4d27afb7e18e4 |
| bionic-probe-v2 | 02dbc40e7ff04eafd633d24b1c8bd69fe88959d9105c5cb8cfe4e78c7a8db9cc |

## 静态 loader 依赖：候选不等于绑定

新增只读 `loader_audit.py`：解析 VERNEED/VERDEF/VERSYM、可见性、版本名和 hash，
清点 REL/RELA/JMPREL、完整写跨度和 init/fini 数组；未知 relocation 宽度和 packed 编码
显式保留，不应用重定位、不调用 resolver、不执行构造器。

```sh
python3 tools/pure_linux/loader_audit.py --baseline "$BASELINE" \
  --supported-engine firstlight_CR/native_runner/supported_engine.json \
  --output "$EVIDENCE/loader-audit-final.json"
```

实际 exit 0。185 个匹配 ARM64 库中，libg 的 473 个导入有 469 个静态候选。
余下 4 个均为 weak：ZSTD_trace_compress_begin/end、ZSTD_trace_decompress_begin/end；
每个各出现一次 GLOB_DAT 和一次 JUMP_SLOT。其是否可忽略仍为 UNRESOLVED，未补造 stub。
版本需求为 libdl/libm/libc 的 LIBC 和 libc 的 LIBC_N。

RELA 116,326 项（ABS64 1,276、GLOB_DAT 29、RELATIVE 115,021）；JMPREL 422 项全为
JUMP_SLOT。起始地址及完整写跨度越界均 0，未知宽度 0，未解码 packed 项为空。
发现 753 个 init_array 和 2 个 fini_array 槽，无 DT_INIT/DT_FINI；这些不是运行覆盖率。
6 个 IFUNC 导出候选未执行 resolver。真实运行时 namespace、查找顺序和符号绑定尚未验证。

## 测试与证据

```sh
python3 -m unittest discover -s tools/pure_linux -p 'test_*.py' -v
python tools/run_main_engine_checks.py
python tools/run_public_checks.py
```

专项 29 项通过，exit 0（0.056 秒）。其余回归在父提交 tracked 快照加新增 loader 工具和
测试的隔离副本运行：主工程 211 项通过（21.79 秒），公开入口 exit 0，主组 794 passed /
27 skipped（48.40 秒）；其他组 23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、
65。集合不累加，跳过项不当作原生通过。该回归副本不含最后 clone 汇编修复，修复由真实
guest v4 重验。新工具专项单独运行，不假称公共入口已纳入这些测试。

原始私有日志归档：guest-baseline.tar、guest-probe-results.tar（失败）、
guest-probe-v4-results.tar、bionic-list-results.tar、loader-audit-final.json、unit-final.*、
run_main_engine_checks.*、run_public_checks.*。完整 SHA、退出码及脱敏命令见
[机器可读证据](pure-linux-guest-abi-evidence-20261027.json)。原始二进制、资源和审计不发布。

## 首个阻塞与下一步

宿主/guest ABI 阻塞已解除。真实 libg 加载前的首个阻塞是 **MISSING_INPUT：锁定原始 APK**。
当前 app manifest 为 8/9；本地同版本 APK 的整包 SHA 不匹配 supported_engine 锁定身份，
历史 app manifest 的 APK 预期另有冲突，未擅自选用或重写任何身份清单。
214/214 sysroot、990/990 update、libg 和 runtime fingerprint 仍匹配。

锁定 APK SHA：`685c115ee432d5fd2d7be856a58ea0cb029c9c187aa9061f3cd250994dfbcf2a`。
取得该输入并闭合 manifest 冲突后，在新目录单独做真实 libg 加载；若成功只报 LOAD_ONLY。
随后依次定位内容 root/context、manager、GameState 初始化及配置/JNI 首个实际调用依赖。
本轮没有进入无 VM 冷启动或真实对局，更没有测得 10×。

## 后续用户输入实验（独立记录）

用户随后提供的同版本APK，8个ARM64库及全部9,829个assets已逐项SHA匹配。
新输入单独登记，历史整包锁定清单保持原样。真实Ubuntu ARM64中已成功加载并卸载
原版libg，exit0，取得LOAD_ONLY；未执行战斗初始化或生命周期。
见[后续报告](PURE_LINUX_USER_APK_LOAD_2026-10-27.md)。本报告先前的NOT_RUN保留为历史记录。
