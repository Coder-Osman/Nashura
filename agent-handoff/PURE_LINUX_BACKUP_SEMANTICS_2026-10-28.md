# E2.1-N：Backup callback 服务端语义闭合调查（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。本阶段基于当前工作树 HEAD
`a49b1ffda7dcc63045cc73ec90d7dfc7eb6599b0`；提交身份以包含本报告的提交为准。

## 结论

配置 native 候选在真实 libg 子进程完成 commit 后主动以 exit96 停止，原因是
Backup callback 尚未闭合。本阶段追踪了真实客户端 ART 历史 trace，并检查匹配
Android 30 `services.jar` 的备份服务方法。发现客户端发起的是同步 Binder 事务；
服务端静态路径包含调用者/用户权限检查、备份目标解析、Handler 入队、内存请求
登记、journal 写入及 `KeyValueBackupJob` 调度。因此将 callback 伪装为 no-op 或
只递增本地计数不满足已发现的服务端语义。

**这不是服务端运行时交付证明。** ART trace 没记录参数或返回值；本机没有启动
Android 服务，也没有真实 Android Binder 端到端观察。服务端 DEX 的结果为
`STATIC_FOUND`，不是 `RUNTIME_OBSERVED`。catalog 严格门仍关闭。

## 证据与范围

### 客户端 ART trace（历史输入重分析）

- 输入是既有、完整的 ART method trace；本轮在全新隔离目录重新运行审计器，没有
  启动 Android Emulator、ART 或游戏进程。
- 文件含 835,263 条记录。观察到 23 个 `requestBackup` 调用，分布在 2 个线程；
  23 次均正常返回，嵌套 scope 平衡，scope 内未知方法 0 条。
- 23 次均进入并正常返回 `BackupManager` 构造、服务 binder 检查、`dataChanged()`、
  `IBackupManager` proxy `dataChanged(String)`；服务发现相关路径在首次使用时记录
  一次，Binder `transact`/native 与 `readException` 各 24 次，Parcel recycle 48 次。
- 客户端方法审计显示 `transact` flags 为 0，即非 oneway，同步请求/回复；事务码为
  2，两个 Parcel 来源独立。该项是匹配 DEX 的静态参数来源分析，不是实际 Binder
  服务接收参数的动态记录。
- trace 不包含 method 参数/返回值，故正常返回只证明被观测 Java 调用返回；不证明
  `dataChanged` 已由目标服务接收、权限通过、写盘或已排程。
- ART trace 中 `getDefaultImpl` 元数据缺失；`Log` 元数据存在但 scope 内无事件。
  分别保留为未覆盖和零观测，不解释成依赖不存在。

### Android 30 服务端 DEX（静态发现）

真实 `services.jar` 解包后，`classes.dex` 与匹配 dexdump 在新隔离目录运行。对 11
个白名单方法生成脱敏直接调用/控制流摘要；原始 DEX、完整 dexdump 和 stderr 留在
本机私有隔离目录，不进入发布仓库。调用图显示：

1. `BackupManagerService.dataChanged` 路径调用按用户/调用者检查并取得
   `UserBackupManagerService`，再将包名交给用户服务。
2. `dataChangedForUser` 查询用户是否 ready 后进入上述用户服务路径。
3. `UserBackupManagerService.dataChanged` 获取调用 PID/UID、解析受影响备份目标，
   并调用 backup handler 的 `post`。
4. `dataChangedImpl` 路径调用 `writeToJournalLocked`，将请求放入待处理结构，并调用
   `KeyValueBackupJob.schedule`。
5. journal 路径创建/复用 journal 并调用 `RandomAccessFile` 的长度、seek、writeUTF、
   close 操作。

这些是 DEX 方法体中的直接调用边；控制流分支的具体运行取决于用户状态、权限、
备份目标和服务状态。本阶段没有解释整个服务状态机，也没有执行真实服务端代码。

## 输入身份与命令

平台归档、DEX 和 dexdump 均在本机私有目录核对。平台归档 SHA 为
`c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`；服务端
`classes.dex` SHA 为 `66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a`；
dexdump SHA 为 `9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692`。
分析前后 DEX 与归档身份一致，`dexdump` exit 0，stderr 为空。历史 ART trace 输入 SHA
为 `01bc15eba14b2ba50b0b5cb66763f1be1ecf4e35265d832f672607264dbf52d2`。

实际命令：

```sh
python3 tools/pure_linux/backup_server_audit.py \
  --dex <PRIVATE_SERVICES_CLASSES_DEX> \
  --dex-sha256 66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a \
  --dexdump <MATCHED_DEXDUMP> \
  --dexdump-sha256 9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692 \
  --output <NEW_PRIVATE_AUDIT_DIR>
# exit 0; report SHA256 4e0c5f8798f80a82b89ae582269b36d331cbe2a3917d6e22ff3dc69c9f09564b

python3 tools/pure_linux/backup_trace_audit.py \
  --trace <EXISTING_PRIVATE_ART_TRACE> \
  --trace-sha256 01bc15eba14b2ba50b0b5cb66763f1be1ecf4e35265d832f672607264dbf52d2 \
  --output <NEW_PRIVATE_TRACE_REPORT>
# exit 0; 835263 records, 23 complete request scopes, 2 threads
# report SHA256 82cd3fdb3a25f2d0874e1c4fba198e61d53077160ad431f3a59f3dfd0f85aacb

python3 -m pytest -q -p no:cacheprovider \
  tools/pure_linux/test_backup_server_audit.py \\
  tools/pure_linux/test_backup_trace_audit.py \\
  tools/pure_linux/test_backup_method_audit.py
# exit 0; 49 passed
```

脱敏机器证据：[`pure-linux-backup-semantics-evidence-20261028.json`](pure-linux-backup-semantics-evidence-20261028.json)。原始数据、地址、寄存器、APK、游戏库、资源及完整反汇编不发布。

### Linux guest callback 探针失败证据

既有 v121 在两个隔离 Android linker/Bionic 子进程中执行，均依次输出
`before_binder_load`、`after_binder_load`、`before_check_service`；随后 stderr 报告
Binder driver `/dev/binder` 无法打开并触发 `SIGABRT`。两个进程都未执行到服务查找
返回。这既不是服务不存在的证据，也没有观测 callback 参数、返回值或副作用。脱敏
运行记录位于源工作区隔离目录的 `v121-backup-service-03k3ttq5-ops-results.json`；
两次子进程均为 `exit -6`，阶段与结果 JSON SHA256 为
`a02c473c92f5a9e24582fc92749e32ab0e6d922129499702bb46380bc5a7c207`。stdout SHA256
均为 `b62d6c0f1a4d73b26ac149c558a77585c2306e107af1ba4e41b4468aad344dcb`；两份 stderr
SHA256 分别为 `47f72c55bf0069c8d9f0e90023a12139ec2cdde3043a3b68fa3bedb19d35e43b` 和
`4f969857796c4641ade2745253f6c5389880b6876467ba150eb6a1a2e8cdebd9`。私有 guest 日志
与 syscall 记录不发布。本轮没有重跑此已知无效路径，也没有触碰运行中的 Android
Emulator。

## 状态表与后续

| 项目 | 状态 | 证据边界 |
|---|---|---|
| 客户端 `requestBackup`/Binder 路径 | `RUNTIME_OBSERVED` | 历史 ART trace；不含参数/返回值 |
| 同步事务、transaction 参数来源 | `STATIC_FOUND` | 当前客户端 DEX；不是动态事务捕获 |
| 服务端权限、handler、journal、job 调用边 | `STATIC_FOUND` | 匹配 Android 30 服务端 DEX；未运行服务 |
| Backup service delivery / 权限结果 / journal 副作用 | `UNRESOLVED` | 需要端到端服务运行证据或受控等价对象图 |
| native 配置 callback 与平台双槽提交一致性 | `UNRESOLVED` | 真实 libg 子进程仍因未闭合 callback exit96 |
| `CATALOG_READY_PASS` | `false / BLOCKED` | 本阶段不改变严格 gate |
| ResourceManager / GameState / 空场 / reset / `GameStateStep` | `NOT_RUN` | 不越级调用 |
| 完整战斗、Android 逐 tick 对照、Android-free、无 VM、10× | `NOT_RUN` | 本轮均未执行 |

下一步先提供或建立能运行真实 Binder driver 与匹配 servicemanager 的隔离 Android
服务端环境，并可观测同步事务目标、参数、权限/用户 ready 结果、返回值及服务端副作用。
当前 Ubuntu guest 缺 `/dev/binder`，本机 macOS host 也没有 Binder 设备节点；运行中的
Emulator 属于其他任务且本轮不使用。当前唯一运行时阻塞是缺少可用于本实验的 Binder-
capable Android 服务端运行环境。不得用静态 DEX 调用边替代动态交付；不得将 callback
默认成功或跳过来放行 catalog。其余可独立工程审计可继续，但 ResourceManager 与
GameState 仍不得越过 catalog 前置门。
