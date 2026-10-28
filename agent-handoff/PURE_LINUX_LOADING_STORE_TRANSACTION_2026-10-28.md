# E2.1-N：受控 loading 配置写入事务（2026-10-28）

本轮在已获准的隔离 AVD `emulator-5580` 上执行一次受控事务：读取
`LOADING_SCREEN_STATE_LABEL` 原值，写入 `datatables-init`，读回后恢复为空值。
实验没有重启应用、清除数据、修改 AVD 配置或替换 APK；不进入 E2.2、ResourceManager、
GameState 或训练。

## 结果

- 实验前读值为空字符串。
- 写入 `datatables-init` 返回成功，等待后同进程读回 `datatables-init`。
- 恢复空字符串返回成功，等待后读回空字符串。
- 两次写入均进入 `GameApp.requestBackup()` 和客户端
  `IBackupManager$Stub$Proxy.dataChanged(package)`。
- 两次请求均在 `system_server` 进入 `BackupManagerService.dataChanged`、
  `dataChangedForUser`、`isUserReadyForBackup`、`UserBackupManagerService.dataChanged`、
  `dataChangedTargets`、异步 `dataChangedImpl`，并触发 `KeyValueBackupJob.schedule`。
- 四个 Backup 快照的 `Pending key/value backup` 均为 30，目标包在事务前已存在于队列；
  因此本轮不能从队列数量区分新增、去重或合并行为。

## 未闭合部分

`writeToJournalLocked`、`DataChangedJournal.addPackage`、`SharedPreferencesImpl` 的
`commitToMemory`、`commit`、`enqueueDiskWrite`、`writeToFile`、`createFileOutputStream`
在本次窗口中没有调用事件。它们保持 `NOT_OBSERVED_IN_THIS_CALL`，不解释为不存在依赖。
对应 hook 已成功安装。`SharedPreferencesImpl.contains` 在两次事务中各出现一次；直接
`c.a.n` 入口没有事件，但其调用栈出现在 `contains`，所以这条边仍需更细的对象/调用方式
观测，不能据此认定 Java 存储已被替换或已完成持久化证明。

本轮读回只证明当前进程可见的状态变化；跨进程重启后的磁盘恢复、双槽文件名与提交顺序、
journal 持久化和 Backup 交付仍为 `UNRESOLVED`。

## 证据与命令

实验输出保存在本机私有目录，不发布原始 Frida 事件或完整 `dumpsys backup`：

`/private/tmp/nashura-loading-store-transaction-20261028-102313/frida-events.jsonl`

```sh
/tmp/nashura-frida-16.7.19-venv/bin/python \
  tools/pure_linux/run_loading_store_once.py \
  --observer tools/pure_linux/backup_runtime_observer.js \
  --output /private/tmp/nashura-loading-store-transaction-20261028-102313/frida-events.jsonl \
  --system-server-pid 522 --game-pid 6901 \
  --adb "$HOME/Library/Android/sdk/platform-tools/adb" \
  --serial emulator-5580 --key LOADING_SCREEN_STATE_LABEL \
  --value datatables-init --restore-value ""
# runner exit 0；外层摘要命令因使用 zsh 保留变量名 status 产生 shell 赋值错误，
# 不影响 runner 已写出的 JSONL；本报告只采用 runner 输出。
```

| 项目 | 结果 |
|---|---|
| 设备/进程身份 | `RUNTIME_OBSERVED`：游戏 PID 6901，`system_server` PID 522 |
| 受控写入与恢复 | `RUNTIME_OBSERVED`：两次调用返回，最终值为空字符串 |
| 客户端 Backup 请求 | `RUNTIME_OBSERVED`：2 次 request/proxy 链 |
| 服务端数据变更 | `RUNTIME_OBSERVED`：2 次异步 dataChangedImpl |
| job 调度 | `RUNTIME_OBSERVED`：2 次 KeyValueBackupJob.schedule |
| journal | `NOT_OBSERVED_IN_THIS_CALL` |
| 平台磁盘提交 | `UNRESOLVED` |
| `CATALOG_READY_PASS` | `BLOCKED`，仍未推进后续对象图 |

## 工程回归

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；531 passed, 1 skipped；5.15s
```

该回归只证明发布的 Python/辅助工具契约；不替代真实 backup 磁盘提交、跨进程恢复、
catalog ready 或 GameState 生命周期证据。

输入与输出摘要见 `pure-linux-loading-store-transaction-evidence-20261028.json`。
