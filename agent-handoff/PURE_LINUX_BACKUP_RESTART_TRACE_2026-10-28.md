# E2.1-N：隔离 AVD 重启期 Binder 观测与 catalog 门复核

日期：2026-10-28。分支 `codex/pure-linux-libg-headless-20261026`，基线
`850c3673df6cd0fa5fde6919593562d9f0fd9919`。本报告补充前序被动 trace；不覆盖旧报告。

## 结论

按用户明确许可，仅在隔离设备 `emulator-5580` 对现有游戏包执行 force-stop 和启动；未清数据、改 AVD 配置或安装应用。进程由 PID `6276` 变为 `6377`，Activity 启动命令成功。捕获窗口内观察到游戏进程发起 Binder 请求，其中43笔目标进程为 `system_server`。Binder driver trace 不提供接口描述符、Parcel 参数或可关联的服务端返回语义；这些事件不能归因为 `BackupManager.dataChanged`。

trace 窗口前后 Backup Manager 均 enabled，pending key/value 数均为30，目标包在两份快照中均处于 pending 队列。计数未变不证明请求被接受或拒绝，也不能区分旧队列项与本次启动副作用。过滤日志未发现可归因于本次重启的 `requestBackup`、`dataChanged`、配置键或目标值记录。

配置实现仍未达到可接入真实 loader 的门槛。既有 `loading_config.c` 是隔离 native 候选；hook 明确标注 `isolated_native_candidate_no_android_backup_parity`。真实 libg 实验曾在完成本地 commit 后以 exit 96 停止，避免把本地计数冒充 Android Backup 副作用。strict catalog 门保持 `false / BLOCKED`。

因此本轮**没有重跑真实 catalog 子进程**：目前没有已验证的配置实现可满足“接入后自然初始化”的前置条件。再次运行已知会因候选守门退出的路径不会闭合契约，且不得将自然 ready 观察提升为 strict pass。ResourceManager、GameState、空战场和 `GameStateStep` 均未运行。

## 动态证据范围

- 设备：Android API 30、ARM64、`emulator-5580`；游戏进程 PID `6276 → 6377`。
- 新 trace：7,063行、1,027,458字节；2,352笔 `binder_transaction` 事件。游戏进程来源事件63笔，其中43笔目标 PID 为 `system_server`。
- 事务目标 PID只标识 Binder 进程，不等价于服务接口；trace 未解码事务码所属接口、调用参数、权限/用户分支、同步返回值或 journal/job 副作用。
- Backup 快照前后均为 enabled、pending key/value=30，目标包均列于队列。该观察不建立队列项目来源或因果关系。
- 日志过滤窗口无可归因的配置写入或 Backup callback 记录；日志缺失不是 callback 不存在的证据。
- trace 和系统快照正文保留在本机隔离目录，不进入仓库；仅发布字节数、摘要和脱敏统计。

## 状态

| 项目 | 状态 | 说明 |
|---|---|---|
| 隔离游戏进程重启 | `RUNTIME_OBSERVED` | 仅 force-stop 与重新启动现有包 |
| 游戏到 system_server Binder 传输 | `RUNTIME_OBSERVED_LIMITED` | 43笔目标 PID匹配；接口和参数未知 |
| Backup callback 服务端接收/返回/副作用 | `UNRESOLVED` | 本 trace 不具备接口解码或因果证据 |
| `LOADING_SCREEN_STATE_LABEL` 完整配置契约 | `BLOCKED` | codec、提交失败语义及 Backup 副作用未闭合 |
| 已验证配置实现接入 libg | `NOT_RUN` | 当前只有明确不具平台等价性的候选 |
| 自然 catalog 重验 / `CATALOG_READY_PASS` | `NOT_RUN / BLOCKED` | 未用候选绕过门禁；strict pass仍为false |
| ResourceManager / GameState / `GameStateStep` | `NOT_RUN` | 等待 catalog strict gate |
| 无 JavaVM、Android-free、完整战斗、10× | `NOT_RUN` | 无相关证据 |

## 下一步唯一阻塞

取得匹配 Android 30 运行时中客户端配置写入到 Backup 服务端的可归因事务证据，覆盖接口/参数、服务端权限与用户状态分支、同步结果及持久化/排程副作用；或以匹配版本源码和运行时证据闭合其等价契约。之后实现并验证完整配置适配，再在两个全新真实 libg 子进程中重跑自然 catalog，并执行 strict gate。当前 Binder 传输计数和 pending 队列快照不足以解除该阻塞。

## 输出身份

机器证据见 [`pure-linux-backup-restart-trace-evidence-20261028.json`](pure-linux-backup-restart-trace-evidence-20261028.json)。原始 trace、logcat 和 dumpsys 文件均未发布。未修改 APK、游戏二进制、资源、旧报告或 `main`；本轮未启动训练或 GameState 实验。
