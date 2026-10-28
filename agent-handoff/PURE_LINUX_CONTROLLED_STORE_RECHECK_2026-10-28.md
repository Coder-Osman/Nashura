# E2.1-N：真实配置写入与原值恢复复核

分支 `codex/pure-linux-libg-headless-20261026`，本轮代码基线
`cdc5fae77aa3ed5f1ac4eaeebebb07be8226792d`。本轮只在既有隔离 Android 30 ARM64
AVD 中执行一次受控 `LOADING_SCREEN_STATE_LABEL` 写入与恢复；不重跑原生 catalog，
不进入 ResourceManager、GameState 或 Step。

## 结果

受控事务前，游戏 API 返回原值 `server-login`。写入
`nashura-controlled-store-20261028` 后读回该值；恢复原值后，API 再次返回
`server-login`。随后独立的只读 observer 再读一次，仍为 `server-login`。
这证明同一应用进程内目标值可写、可读并恢复；未重启进程，因此不证明跨进程磁盘恢复。

Frida trace 中的 83 个 Java call scope 全部配对，所需 hook 已安装，没有 observer、
调用或恢复错误。目标写入和恢复各触发两次 `c.a.n` 双槽写入；四次
`SharedPreferences.Editor.commit()` 均返回 true，每个槽均观察到
`commitToMemory → enqueueDiskWrite → writeToFile → createFileOutputStream`。
每次双槽提交后都观察到 `GameApp.requestBackup()` 和客户端 Binder proxy。
`system_server` 中两次 `UserBackupManagerService.dataChangedImpl` 和两次
`KeyValueBackupJob.schedule` 均实际进入。

本次开始前目标包已在 30 项 pending key/value backup 中；事务前、目标写入后及恢复后
队列计数均为 30，且没有运行中的 backup。`writeToJournalLocked` 在本窗口内未观测到。
结合既有 pending 成员，这与重复请求跳过 journal 的路径相容，但不能归因证明本次
请求的具体分支、journal 持久化、服务交付或 Linux callback 等价性。

首次执行尝试因 AVD 冷启动后 Frida server 未运行，在 attach 阶段退出 1；该次只采集
了 Backup 状态，没有触发配置读取或写入。随后启动 AVD 中已存在且与 host 版本一致的
Frida server 16.7.19，只读 preflight 通过，再执行上述唯一一次目标写入/恢复。
原始 JSONL 以 `0600` 保存在本机隔离临时目录，不发布原始设备日志或偏好文件。

## 环境与身份

- AVD：既有 `nashura-e2-backup-api30`，serial `emulator-5580`，Android API 30，
  ABI `arm64-v8a`，Android 11；冷启动时没有可用的 `default_boot` snapshot，没有清数据。
- 游戏包：`nullsroyale.rel.free`，versionName `15.535.13`，versionCode `150535013`；
  安装包字节 SHA 未在本轮重新捕获，不把版本号写成 APK 身份证明。
- 本轮 PID：`system_server=503`，游戏进程 `4833`；写入前脚本验证 PID、cmdline 和
  Frida process identity。
- AVD `config.ini` SHA-256 前后均为
  `67e43b0ccaa7b80b3fec363308b1fb51e6c93ce5bde2d7e24dfe01a644e3c37c`。
- observer SHA-256：`fe4323bf9c2d0afaa87fc10121ba8ad0068dbf36fd768de5eba87ea4fe987013`；
  runner SHA-256：`aacae07aa2725153a4294e4d4e3579d356d7c8150050a97115072306b2540a81`。
- AVD 已通过 `adb emu kill` 正常关闭，当前设备列表为空；未安装/替换 APK、清数据或
  修改 AVD 配置。

## 命令与证据

目标写入命令：

```sh
/tmp/nashura-frida-16.7.19-venv/bin/python \
  tools/pure_linux/run_loading_store_once.py \
  --observer tools/pure_linux/backup_runtime_observer.js \
  --output <PRIVATE_OUTPUT>/controlled-store.jsonl \
  --game-pid 4833 --system-server-pid 503 \
  --adb <ANDROID_SDK>/platform-tools/adb --serial emulator-5580 \
  --key LOADING_SCREEN_STATE_LABEL \
  --value nashura-controlled-store-20261028 --execute-store
# exit 0；读取、目标写入、原值恢复和末值读取均完成
```

只读终值复核使用同一 runner 的 `--observe-only`，exit 0，仍返回 `server-login`。
事务 trace SHA-256 为 `4847b0093f91e4ef3ed114fa895104a90e0f7c9cd953189b90c4b9ba40981d5d`；
终值复核 trace SHA-256 为
`5480881bb695edb667f072f6fb866a6c5664520afbd0d360cb503dab9b529614`。首次 attach 失败
记录 SHA-256 为 `65897464d00af6e3a2bfa57bd3929a317cd1db953b27d21d36e8ebbf0ab6e998`。
这些是本机私有审计流的身份摘要，不包含原始日志。

严格 `loading_store_audit.py` 需要事务四阶段的偏好文件字节快照。本次 runner 没传
`--preference-file`，因为普通 ADB shell 无权读取应用私有目录；因此没有运行或冒充
该严格审计结果。root 只读检查了末态文件，但没有写前和目标态字节，不能验证本次
transaction 的 XML 中间态或文件逐字节恢复。

专项验证命令：

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；953 passed, 1 skipped
```

发布检查结果：`tools/run_main_engine_checks.py` exit 0（211 passed）；
`tools/run_public_checks.py` exit 0（1,104 passed, 42 skipped）；
`tools/check_public_snapshot.py` exit 0（2,583 files）；
`gitleaks dir . --config .gitleaks.toml --redact` exit 0（no leaks found）；
`git diff --check` exit 0。公开回归生成的检查 XML 是本轮当前测试输出。

## 门状态与下一步

本轮取得 **CONTROLLED_STORE_RESTORE_RUNTIME_PASS_LIMITED**，仅覆盖当前 Android 进程中的
API 读回、双槽 commit 调用及 Backup 请求到服务端调度路径。未验证跨进程恢复、
本次偏好文件中间态、journal 新增/去重归因、Backup 交付、Linux callback 等价性或
完整 catalog 资源语义。严格 `CATALOG_READY_PASS=false`；ResourceManager、GameState、
`GameStateStep`、完整战斗、no-VM、Android-free 和 10× 均未运行。

下一步先以只读方式完善当前非空偏好材料的身份/持久化证据，并将观测到的 pending
去重分支与 Linux candidate 的 Backup 所有权逐项绑定；未通过严格 catalog 门前不推进
战斗对象图。
