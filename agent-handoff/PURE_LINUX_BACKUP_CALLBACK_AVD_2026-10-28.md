# E2.1-N：隔离 AVD Backup callback 触发观察（2026-10-28）

## 结论

在用户允许启动的隔离 `astral-api30` AVD 上，对预装游戏执行一次冷启动观察。游戏成功启动并运行至少 45 秒，但没有观察到可归因于该启动的 `requestBackup`、`dataChanged` 或 BackupManager 队列变化。设备启动前已有该包的待处理请求，因此队列存在本身不构成本次调用证据。

**本次未闭合 callback；catalog 严格门仍为 BLOCKED。** 这次运行也不是匹配输入 APK 的原生 callback 证据：设备预装包与用户提供 APK 的整包 SHA 不同。没有安装/替换 APK、没有改动 backup 设置或应用数据；观测结束后仅停止本次启动的应用并关闭 `astral-api30`。另一个 `e2-1l-only` AVD 未触碰。

## 环境和身份

| 项目 | 观测 |
|---|---|
| AVD | `astral-api30`，Android 11 / API 30，设备 ABI `arm64-v8a` |
| 设备 | `emulator-5554`，启动完成 |
| 包 | `nullsroyale.rel.free`，versionName `15.535.13`，versionCode `150535013` |
| 包标志 | `ALLOW_BACKUP` |
| 设备 `base.apk` SHA-256 | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| 用户提供整包 APK SHA-256 | `a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f` |
| 身份结论 | 不同 APK 文件身份；不能据版本号相同推断字节相同 |
| Backup 初态 | `Backup Manager is disabled`；启动前 dumpsys 已有该包的 pending key/value 请求 |

设备侧包身份只读取文件 SHA，没有 pull APK 或发布 APK 内容。

## 实际操作及结果

命令均在本机 Android SDK `platform-tools/adb` 上定向到 `emulator-5554` 执行：

```sh
adb devices -l
adb -s emulator-5554 shell getprop sys.boot_completed
adb -s emulator-5554 shell getprop ro.build.version.release
adb -s emulator-5554 shell getprop ro.build.version.sdk
adb -s emulator-5554 shell getprop ro.product.cpu.abi
adb -s emulator-5554 shell pm path nullsroyale.rel.free
adb -s emulator-5554 shell sha256sum <device-base.apk>
adb -s emulator-5554 shell dumpsys package nullsroyale.rel.free
adb -s emulator-5554 shell dumpsys backup
adb -s emulator-5554 shell am start -W -n nullsroyale.rel.free/com.supercell.clashroyale.GameApp
```

- 设备、包元数据和 SHA 查询：exit 0。
- 首次启动尝试使用不完整组件名，返回 `IllegalArgumentException`；没有启动应用。原始失败输出保留在本机隔离输出目录。
- 完整组件名冷启动：`Status: ok`、`LaunchState: COLD`，应用 PID `4568`；恢复 Activity 为目标游戏 Activity。
- 观察窗口：启动后 45 秒。logcat 出现 Activity 启动记录，但没有目标 `requestBackup` / `dataChanged` 记录。
- BackupManager dumpsys 前后除动态时间字段外无差异；disabled 状态不变；原 pending 队列未清理，不能据其存在归因本次启动。
- 观测结束后执行 `am force-stop` 并定向 `adb emu kill`；该 AVD 进程退出，`adb devices -l` 不再列出设备。
- 本轮未启用 Backup、未运行 `bmgr backupnow`，也未将其他调用冒充客户端 callback。

## 隔离输出摘要

原始设备输出与 logcat 保存在本机 `/tmp/nashura-backup-callback-20261028-073239/`，不发布原始日志。摘要文件 SHA-256：

| 文件 | SHA-256 |
|---|---|
| `launch-corrected.txt` | `24947613d00c899bda22167a4b3d0a817f360f21c28c4f607408c6a2b6691a0f` |
| `backup-prelaunch.txt` | `61afce8318dd7b82a788fb7b5d23baecb3ad4a9bd9340d05c857dfa850c9b39b` |
| `backup-postlaunch.txt` | `1ccadbafe7f47d4e4ac0d27bbbfb1c9c540e8d22c97750689cfdd4f0432875cc` |
| `activity-postlaunch.txt` | `a82de52218cc3f2926010ed90f2574ad871d2b9fcab2caaa52d4889156cbcfa8` |
| `logcat-postlaunch.txt` | `b0226c8d776bcfa3b82b0faa2a4183b68d88072ac2be1fbf59c21c65b0d3965d` |

完整脱敏机器摘要见 `pure-linux-backup-callback-avd-evidence-20261028.json`。SHA 仅绑定本机私有原始输出；公开报告不包含其中内容。

## 状态

| 项目 | 状态 | 说明 |
|---|---|---|
| AVD 环境启动 | `RUNTIME_OBSERVED` | 隔离 API 30 ARM64 AVD 启动完成 |
| 游戏 Activity 冷启动 | `RUNTIME_OBSERVED` | 预装包运行成功，文件身份与指定 APK 不同 |
| 客户端触发 `requestBackup` | `NOT_OBSERVED` | 45 秒窗口无匹配 log 或 Backup 状态增量；不代表路径不存在 |
| BackupManager 服务状态 | `RUNTIME_OBSERVED` | dumpsys 明示 disabled，旧 pending 项在启动前已存在 |
| 同步 Binder callback 到服务端 | `UNRESOLVED` | 本实验未产生可归因请求，未证明事务交付、权限结果或 journal 副作用 |
| strict catalog gate | `BLOCKED` | 不调用 ResourceManager / GameState |
| 本地 AVD 清理 | `PASS` | 本轮应用停止，`astral-api30` 退出；其他 AVD 未触碰 |

## 下一步

继续追踪真实配置写入在匹配 libg 输入中的触发条件，并确认如何在匹配 APK 身份、隔离 app 数据和可观测服务端副作用的环境中触发。现有 AVD 的预装 APK SHA 不匹配，且 Backup Manager disabled；不据本次实验替换 callback、放行 catalog 或进入 GameState。
