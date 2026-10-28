# E2.1-N：人工触发 Backup callback 运行时追踪（2026-10-28）

分支 `codex/pure-linux-libg-headless-20261026`，基线 HEAD
`850c3673df6cd0fa5fde6919593562d9f0fd9919`。本阶段只验证一次显式的
`GameApp.requestBackup()`，不把它记作自然启动行为，也不声称配置契约或 catalog 门通过。

## 结果

- 隔离 AVD `nashura-e2-backup-api30` / `emulator-5580` 保持运行；目标游戏 PID `6901`，
  `/proc/6901/cmdline` 为 `nullsroyale.rel.free`、UID `10166`；`system_server` PID `522`，
  `/proc/522/cmdline` 为 `system_server`、UID `1000`。设备 API 30。实验期间未重启进程、
  清理数据或修改 AVD 配置。
- observer 通过 UID 及 `ActivityThread.currentProcessName()` 组合识别 system_server：
  运行时值为 UID `1000` 且 processName `null`。应用进程则报告包名及 UID `10166`。
- 客户端使用 DEX 已确认的对象路径 `GameApp.getInstance().requestBackup()`，仅调用一次。
  客户端 `requestBackup()` 和 `IBackupManager$Stub$Proxy.dataChanged(package)` 均正常返回。
- 同一事务在 system_server 中实际进入 `BackupManagerService.dataChanged`、
  `dataChangedForUser`、`isUserReadyForBackup`、`UserBackupManagerService.dataChanged`、
  `dataChangedTargets`、`dataChangedImpl`，并正常返回。服务端异步 `dataChangedImpl`
  的调用栈来自 `BackupHandler` 工作线程。
- 此次窗口内未观察到 `writeToJournalLocked`、`DataChangedJournal.addPackage` 或
  `KeyValueBackupJob.schedule`。请求前后 `Pending key/value backup` 均为30，且均显示
  `No backups running`。设备在调用前已有30项 pending，因此不能据此区分去重、既有项合并
  或其他分支；journal/job 副作用保持 `UNRESOLVED_NOT_OBSERVED`。
- 前几次 observer 适配尝试均在真实方法入口前失败，未调用原方法；完整错误与输出保留在
  本机隔离目录，不能与成功调用混为一谈。

## 实验材料与验证

原始设备快照、Frida JSONL 和 host runner 输出留在权限为用户私有的
`/tmp/nashura-backup-callback-20261028-094333-confirmed/`，本报告不包含原始设备日志。
其 SHA-256 和汇总计数记录在相邻 JSON 证据文件中。

| 检查 | 结果 |
|---|---|
| Frida host/server | 16.7.19，API 30 ARM64 AVD；两个目标 PID 均成功 attach |
| observer 语法 | `node --check tools/pure_linux/backup_runtime_observer.js`，exit 0 |
| 真实调用入口 | `GameApp.requestBackup()`，1次 begin、1次 return |
| Binder/client 与服务端 | proxy `dataChanged` 及服务端接收/分发路径均有栈和包名证据 |
| journal / job | 本次未观测到写入或排程；不得推断从未执行或语义上不需要 |
| `CATALOG_READY_PASS` | 仍为 `false / BLOCKED` |

## 下一步与边界

此结果闭合“显式请求从游戏实例抵达 Backup Manager 并进入服务端数据变更处理”的运行时链，
但没有证明配置写入触发时的 journal commit/backup 副作用。下一步应在保持同一 APK、原
初始化顺序和同一 AVD 条件下，对真实 `LOADING_SCREEN_STATE_LABEL` store 路径做一次有界
事务观测，捕获 preference 双槽读写、commit、backup callback 与 journal/job；不能用本次
预先已有的 pending 队列推导其效果。

本阶段没有替换配置桥、没有写 ready 标志、没有运行 ResourceManager、GameState 或
`GameStateStep`，也没有启动 no-VM、Android-free、训练或性能实验。
