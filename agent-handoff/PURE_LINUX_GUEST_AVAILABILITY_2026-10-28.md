# E2.1-N：ARM64 guest 运行现场核验（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。本报告仅记录只读环境检查，
不改变既有 probe、guest 配置、历史报告或实验状态。

## 结果

| 检查项 | 结果 | 证据边界 |
|---|---|---|
| Guest 架构 | `aarch64` | 通过 VMware Tools 在现有 Ubuntu guest 只读查询 |
| Kernel | `6.8.0-138-generic` | 只读查询 |
| 根盘可用空间 | `1,100,648,448` bytes | `df -B1 /`；当前余量有限 |
| `/dev/binder` | 不存在 | 文件存在性检查及 guest `ls` 输出 |
| `/dev/vndbinder` | 不存在 | 文件存在性检查及 guest `ls` 输出 |
| `binder_linux.ko` | 存在但未加载 | `modinfo`、模块目录及 `/proc/modules` 只读检查 |
| binderfs | 未挂载 | `/proc/mounts` 中无 binderfs 项 |
| Android `servicemanager` | 未运行 | guest 进程列表未发现 `servicemanager` / `vndservicemanager` |
| 既有 probe PID 2569 | `T (stopped)`，`PPid=1`，`TracerPid=0`，约7小时26分 | 命令为既有 `linker64`/`libg.so` probe v27；未附加、继续或终止 |
| 既有 probe PID 2595 | `T (stopped)`，`PPid=1`，`TracerPid=0`，约7小时19分 | 命令为既有 `linker64`/`libg.so` probe v29；未附加、继续或终止 |

两进程处于内核停止态且没有 tracer，但仍保留原样；本次没有证据证明其所有者或可回收性，
因此不清理。guest 中不存在 Binder 设备，故当前无法在该环境执行真实 callback 服务交付。

后续只读核验发现内核模块文件 `/lib/modules/6.8.0-138-generic/kernel/drivers/android/binder_linux.ko`
存在，但当前未加载；这不提供 Binder 设备或 Android 服务端。模块检查输出 SHA-256 为
`a6d2781dd2f45808fae961510983df02abdcc2d4882aecdc0adf92d24c63d0af`。本机
`ANDROID_HOME`、`ANDROID_SDK_ROOT` 未设置；当时的常见目录扫描范围有限，不能据此推断默认
SDK 目录中没有 system image。本轮未加载模块、挂载 binderfs、安装系统组件或启动 Emulator。

本轮续验时 Fusion 已有 Ubuntu guest 在线。再次通过 `vmrun runScriptInGuest` 只读核对
`uname -m`、`/dev/binder`、`/dev/vndbinder`、binderfs、`/proc/modules` 与服务进程：
架构仍为 `aarch64`，两个 Binder 节点均不存在、binderfs 未挂载、模块未加载，未观察到
Android `servicemanager`。vmrun 返回0；临时输出 SHA-256 为
`2404486d3b894fc4c5155b3f514e4bf819bdae68633c5b10666764ca72890ef7`，临时文件已清除。
VM 原本已运行，本次未启动或重配置，也未触碰既有 probe。该复核确认 callback 的运行时
阻塞仍在，不改变 catalog、ResourceManager 或 GameState 的状态。

## 检查与清理

使用 Fusion `vmrun` 的只读 `list`、`listProcessesInGuest`、`fileExistsInGuest` 和
`runScriptInGuest` 查询。为读取 guest stdout，创建唯一 `/tmp` 状态文件，复制到 host
临时目录后删除 guest/host 临时文件。没有启动、停止、恢复、重置或附加任何 VM/进程。
模块复核使用 `vmrun runScriptInGuest` 检查 `modinfo binder_linux`、模块目录、
`/proc/modules`、`/proc/mounts` 和服务进程；退出码0，输出SHA见上。早先 SDK 检查为只读，
但只覆盖常见路径和较浅目录层级；后续在默认 SDK 路径发现的镜像见下节，不将旧扫描阴性
结果解释为系统镜像不存在。

本机专项回归：

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
```

结果：exit 0，`531 passed, 1 skipped`。该结果仅验证工程测试，不代表 Binder、catalog、
ResourceManager、GameState 或原生生命周期通过。

## 状态与后续

- `CATALOG_READY_PASS`：仍为 `BLOCKED`；配置/Android callback 语义门未闭合。
- `RESOURCE_MANAGER_PASS`、GameState、空场、reset、真实 `GameStateStep`：`NOT_RUN`。
- 完整战斗、Android 逐 tick 对照、无 VM、Android-free、性能目标：`NOT_RUN`。
- 当前运行时前置条件：取得带真实 Binder 驱动及匹配 Android servicemanager/Backup 服务的隔离环境。单独加载本 guest 已有的 Binder 模块不足以证明服务端交付；本轮不加载模块或启动 Android Emulator。两个停止态 probe 保持原样。

机器可读摘要见 [`pure-linux-guest-availability-evidence-20261028.json`](pure-linux-guest-availability-evidence-20261028.json)。

## Host Android 镜像补充核验

后续只读检查发现默认路径 `/Users/gujiga/Library/Android/sdk` 已安装 Android 29 与
Android 30 的 `google_apis/arm64-v8a` system image；环境变量未配置不代表 SDK 或镜像不存在。
`astral-api30` AVD 配置目标为 `android-30`、`arm64-v8a`、Google APIs，配置文件只读检查。
镜像身份如下：

| 文件 | SHA-256 |
|---|---|
| Android 30 `system.img` | `b8a505ecfc13629aee537f7fa9dcafa0d8d66802a06707f40dac69591e46f70f` |
| Android 30 `vendor.img` | `1953bc61224565d08deac8624502b676b30712fd8bd6c7ee95e415a1d8015a09` |
| Android 30 `ramdisk.img` | `b2453dcc78aaf99a521eb05b34f3b3d774d781b4283874ca72ecd174f2013930` |

当前 `adb devices -l` 为空，未发现运行中的 emulator/qemu 进程；Fusion 只列出上述既有
Ubuntu ARM64 VM。没有启动或连接 AVD。已安装的镜像只是潜在隔离运行时输入，不证明 Binder
设备、servicemanager 或 Backup callback 已运行；当前任务明确不启动 Android Emulator，故
`CATALOG_READY_PASS` 仍被真实 callback 交付证据阻塞。
