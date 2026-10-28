# E2.1-N：Backup Binder 被动窗口观测（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。基于 HEAD
`850c3673df6cd0fa5fde6919593562d9f0fd9919`。本轮仅在既有隔离 API 30 ARM64 AVD
进行五秒 `binder_driver` 被动 trace；未重启/停止游戏进程、未清理应用数据、未发起
backup 请求、未修改 AVD 配置，也未触碰其他设备。

## 结果

trace 有89笔 Binder transaction，目标游戏 PID `5751` 的 transaction 为0；trace 文本
没有 `backup`、`dataChanged` 或 `BackupManager` 字样。设备 `system_server` PID 为522。
游戏进程在捕获期间仍为5751。设备 Backup 状态快照仍显示服务 enabled，pending
key/value 为30，且包含目标包；pending 项不能关联到本捕获窗口。

这是**有限负证据**：被动窗口没有覆盖到可归因的配置 callback，且内核 Binder trace
本身不解码事务包名、调用者权限结果、同步返回值或 journal/job 副作用。它不证明
callback 不存在，也不闭合客户端 request 与服务端副作用。

## 执行与输出身份

环境：`emulator-5580`，Android API 30，ARM64；包身份沿用前序报告核验结果。本轮只读
检查 boot、PID、Activity、Backup 状态和 atrace category；只启动/停止短时内核 tracing。

实际命令序列：

```sh
adb -s emulator-5580 shell atrace --async_start -b 4096 binder_driver
sleep 5
adb -s emulator-5580 shell atrace --async_dump binder_driver
adb -s emulator-5580 shell atrace --async_stop binder_driver
adb -s emulator-5580 shell pidof nullsroyale.rel.free
adb -s emulator-5580 shell dumpsys backup
```

所有实际命令退出码为0；捕获文件 39,681 bytes，SHA-256：
`648c9e7b62a9eab2cd0e9cc1aaa29991922f66f4fb5892be3d6ddf32fc7414d8`。只公开长度和
摘要；trace 原文及状态快照留在本机隔离目录，不进入仓库。trace 不是持久化证据，也
不包含 payload 明文。

本地专项回归命令：

```sh
python3 -m pytest -q -p no:cacheprovider \
  tools/pure_linux/test_preference_codec.py \
  tools/pure_linux/test_preference_contract.py \
  tools/pure_linux/test_preference_store.py \
  tools/pure_linux/test_preference_sequence_job.py \
  tools/pure_linux/test_preference_sequence_capsule.py \
  tools/pure_linux/test_backup_server_audit.py \
  tools/pure_linux/test_backup_trace_audit.py \
  tools/pure_linux/test_backup_method_audit.py
# exit 0; 141 passed in 1.74s
```

此回归只验证配置候选和审计工具，不提升 Android 平台或真实 `libg` 语义状态。

机器摘要见 [`pure-linux-backup-passive-trace-evidence-20261028.json`](pure-linux-backup-passive-trace-evidence-20261028.json)。

## 门状态与下一步

| 项目 | 状态 |
|---|---|
| 短时 Binder driver 捕获 | `RUNTIME_OBSERVED`，限定捕获通过 |
| 目标游戏 callback | `NOT_OBSERVED`，该窗口未覆盖 |
| 服务端接收、权限/用户分支、同步返回及 journal/job | `UNRESOLVED` |
| 配置双槽/codec 候选 | 既有独立测试通过；不等于平台等价 |
| 真实 libg 配置桥接 | 既有候选因 backup callback 未闭合而显式停止 |
| `CATALOG_READY_PASS` | `BLOCKED` |
| ResourceManager / GameState / `GameStateStep` | `NOT_RUN` |

要继续闭合唯一运行时边界，需要在配置写入发生时捕获应用进程到 Backup 服务的完整
同步事务及服务端副作用。当前游戏仍前台运行；本轮不为获取事件而重启它。配置契约
未闭合前，不把既有自然 ready 观察提升为严格门，也不进入 ResourceManager 或战斗
对象图。
