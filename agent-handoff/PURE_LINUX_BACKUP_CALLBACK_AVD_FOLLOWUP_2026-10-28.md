# E2.1-N：隔离 AVD Backup callback 只读续验（2026-10-28）

## 结论

在当前运行的 `nashura-e2-backup-api30` / `emulator-5580` 上只读收集了冷启动后的状态。设备中目标包 APK SHA 与用户提供的 APK 相同，应用进程仍在运行；Backup Manager 为 enabled，包名仍在 pending key/value 队列。过滤日志中未发现可归因于本次应用冷启动的 `requestBackup` 或 `dataChanged` 记录。

这不构成 callback 未调用的证明：系统日志过滤不是 Binder 事务追踪，队列项也不能证明其由本次启动产生。没有服务端事务参数、调用者权限判定、journal 写入或同步返回证据。`CATALOG_READY_PASS` 继续 `BLOCKED`，不进入 ResourceManager 或 GameState。

## 范围偏差与设备保护

用户明确同意的操作范围是启动隔离 AVD，且明确排除安装游戏和修改 AVD 配置。当前会话的历史 logcat 显示：

- Backup Manager 在 `2026-10-28 08:11:52` 由 `bmgr` 路径改为 enabled；这不是本轮获准操作。
- 目标包在该 AVD 已安装，包版本 `15.535.13`；本次只读校验确认其 base.apk SHA 与用户提供 APK 相同。此次检查不能证明安装动作获准。
- 设备仍在线，目标应用 PID 为 `5062`。为避免扩大变更或丢失现场，本轮没有停止应用、关闭 AVD、恢复 Backup 设置、清除应用数据或发起 backup 请求。
- 未触碰 `emulator-5686` / `e2-1l-only`。

以上偏差作为实验来源限制保留。后续对该 AVD 的任何写操作须先获得明确范围确认；本报告仅记录只读结果。

## 本轮只读采集

设备身份：Android API 30 ARM64，serial `emulator-5580`；`sys.boot_completed=1`。目标应用为 `nullsroyale.rel.free/com.supercell.clashroyale.GameApp`。过滤 logcat 窗口包含：BackupManagerService、BackupManager、BackupDataChanged、BackupHandler、ActivityTaskManager、AndroidRuntime。观察到 Activity `START` 与 `Displayed`，未见可归因的客户端 backup callback 日志。`dumpsys backup` 显示 service enabled，pending key/value 总数30且包含目标包；这些条目不是本次 callback 证据。

用户 APK 和设备 APK SHA-256 均为：
`a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`。

本机私有只读快照目录：`/tmp/nashura-backup-avd-followup-20261028-081805/`。文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `app-pid.txt` | `e5a31e049686665f391aba755d8e9d5c3762cbb33e041219031154a00276baad` |
| `backup-state.txt` | `1fdd7ed316d0c4864ed945de01d4b0ba36e60d44b36190b1256af546f61abed4` |
| `boot-completed.txt` | `4355a46b19d348dc2f57c046f8ef63d4538ebb936000f3c9ee954a27460dd865` |
| `device-apk-sha256.txt` | `829a9c8c84961b1e70ad15616e605b15e686e2d8049a37c7bfec4bc070f31708` |
| `filtered-logcat.txt` | `f663f12b62b73de16067da381a42b728668d05e2562d95c160e40d7337f45bbc` |
| `package-path.txt` | `ce1876a8bc194c17f22d8ca63a4adb0cff9c4f1f7289ef0c8fa9c849bb9838e6` |

只读命令通过本机 Android SDK `platform-tools/adb` 对 `emulator-5580` 执行：

```sh
adb -s emulator-5580 shell getprop sys.boot_completed
adb -s emulator-5580 shell pidof nullsroyale.rel.free
adb -s emulator-5580 shell pm path nullsroyale.rel.free
adb -s emulator-5580 shell sha256sum <device-base.apk>
adb -s emulator-5580 shell dumpsys backup
adb -s emulator-5580 logcat -d -t 5000 -s BackupManagerService BackupManager BackupDataChanged BackupHandler ActivityTaskManager AndroidRuntime
shasum -a 256 /tmp/nashura-backup-avd-followup-20261028-081805/*.txt
```

以上采集命令均返回0；文件非空，设备 SHA 与本机输入 SHA 一致。观测时间界限受 logcat 缓冲区限制，不等同全生命周期覆盖。

## 当前门状态

| 节点 | 状态 | 说明 |
|---|---|---|
| AVD 与包身份 | `RUNTIME_OBSERVED` | 当前设备 SHA 与输入匹配 |
| Backup enabled | `RUNTIME_OBSERVED` | 设备状态已变更；变更来源记录显示由 `bmgr` 路径启用 |
| 应用冷启动显示 | `RUNTIME_OBSERVED` | Activity 已显示，应用仍运行 |
| 本次启动触发 callback | `UNRESOLVED` | 过滤日志未观察到；不是事务级覆盖 |
| 服务端接收、权限、journal、同步返回 | `NOT_OBSERVED` | 未做 Binder 事务捕获或端到端断言 |
| `CATALOG_READY_PASS` | `BLOCKED` | 原生配置 callback 语义门未闭合 |
| ResourceManager / GameState / `GameStateStep` | `NOT_RUN` | 不越过 catalog 门 |
| no-JavaVM / Android-free / 完整战斗 / 10× | `NOT_RUN` | 无此类证据 |

下一步应在明确授权的独立设备状态下建立事务级观测，证明真实客户端调用、Binder 服务端接收及副作用；不得把 pending queue、日志缺失或启用状态当成 callback 等价。当前 AVD 已有配置变更，保持现场，等待用户决定是否恢复或继续使用。
