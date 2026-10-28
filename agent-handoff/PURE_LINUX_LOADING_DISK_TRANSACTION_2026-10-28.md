# E2.1-N：真实 loading 双槽磁盘事务（2026-10-28）

## 状态与范围

当前分支 `codex/pure-linux-libg-headless-20261026`，工作基线
`e03179773349fe41e8de5e6fc0606291be4f9688`。本轮完成用户批准的一次
`LOADING_SCREEN_STATE_LABEL` 受控写入和原值恢复，随后只读重验归档。
状态为 `RUNTIME_OBSERVED`：真实 Android 同步提交链与文件结果已观测；
完整配置契约仍 `UNRESOLVED`，严格 `CATALOG_READY_PASS=false`。

旧观察器遗漏和历史失败报告保持原样。本报告补充修复作用域后的独立实验，
不将历史零计数解释为没有写盘，不重写历史证据。

## 环境和身份边界

- 宿主为 Darwin ARM64、macOS 15.6、内存 38,654,705,664 bytes。
- 实验使用独立 Android 30 ARM64 AVD `emulator-5580`，保留 Java VM 和现有游戏进程。
- 归档中的游戏 PID 为 6901，system_server PID 为 522；这是当次实验身份，不是实时 PID。
- 真实 libg 的既有参考 SHA 为
  `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
  本次事务没有重新采集引擎及资源的前后完整身份，因此显式标为
  `NOT_CAPTURED_THIS_TRANSACTION`，参考 SHA 不替代本次身份验证。
- 实验未重启游戏、清数据、修改 AVD 配置或推进 Linux catalog。
  APK、libg、Provider 和 update 输入没有纳入本轮修改。

## 实际观察

值依次为空串、`datatables-init`、空串。归档含 251 条记录，runner exit 0。
两次顶层 store 分别执行两个槽位，每次严格按 `storage.xml → storage_new.xml` 顺序：

| 事务 | 槽位 | 编码编辑 | commit 返回 | writeToFile |
|---|---|---|---|---|
| 目标写入 | storage.xml | putString | true | 正常返回 |
| 目标写入 | storage_new.xml | putString | true | 正常返回 |
| 恢复 | storage.xml | remove 同一个编码键 | true | 正常返回 |
| 恢复 | storage_new.xml | remove 同一个编码键 | true | 正常返回 |

四次均观测到同线程嵌套的
`commit → commitToMemory → enqueueDiskWrite → writeToFile → createFileOutputStream`。
文件名、编辑的编码键/值和落盘 XML 条目吻合。公开证据只保留计数和哈希，
编码键、密文内容和 XML 留在本机私有归档。

写入前与恢复后的两个 XML 各 65 bytes，SHA 均为
`3325d2a819fdd8062c2cdc48a09b995c9b012915bcdf88b1cf9742a7f057c793`。
目标写入后各 168 bytes，SHA 分别为
`f75951c5d2103bfb954404d1419c8a74744d38d3d9fd241dccf97e30c86c1b9d` 和
`a3f8ab679b55cb4426bf19275238b3c4097448e199e436ab95734a5457a80ece`。
恢复结果是逐字节相同，且未改变其他 XML 条目。

四阶段文件快照里的两个 `.bak` 均缺失。这只说明采样时刻的状态，
不证明提交期间从未生成 `.bak`。

每次双槽同步 commit 完成后，才触发一次客户端 `requestBackup` 和 Binder proxy
`dataChanged`，共两次。服务端相关方法的调用计数保留在 JSON 中；
跨进程仅有时间相关性，没有 Binder transaction ID，因此不作逐请求因果归因。

## 代码与离线检查

`run_loading_store_once.py` 增加四阶段只读文件捕获、独占输出和 0600 权限。
新 `loading_store_audit.py` 绑定 trace SHA，核验调用栈/PID/线程、事务顺序、
文件阶段位置、同步磁盘链、双槽顺序、编码编辑与 XML、恢复及 Backup 顺序。
缺 hook、观察错误、未归因写盘、文件身份不符等直接失败；不会连接设备或再次写入。
28 项新增 synthetic 用例只验证审计器，不替代 Android 原生证据。

以下按归档和当次 runner 重建等效参数，使用当次已核验 PID；
这是实验参数记录，**不是重跑指令**，也不是原始 shell 文本归档：

```sh
/tmp/nashura-frida-16.7.19-venv/bin/python tools/pure_linux/run_loading_store_once.py \
  --observer tools/pure_linux/backup_runtime_observer.js \
  --output /private/tmp/nashura-loading-controlled-20261028-99okjE/frida-events.jsonl \
  --system-server-pid 522 --game-pid 6901 \
  --adb "$HOME/Library/Android/sdk/platform-tools/adb" \
  --serial emulator-5580 --key LOADING_SCREEN_STATE_LABEL \
  --value datatables-init --restore-value "" --execute-store \
  --preference-file /data/data/nullsroyale.rel.free/shared_prefs/storage.xml \
  --preference-file /data/data/nullsroyale.rel.free/shared_prefs/storage_new.xml \
  --preference-file /data/data/nullsroyale.rel.free/shared_prefs/storage.xml.bak \
  --preference-file /data/data/nullsroyale.rel.free/shared_prefs/storage_new.xml.bak
# exit 0；原值已恢复；本轮授权的写入实验已完成

/opt/anaconda3/bin/python3 tools/pure_linux/loading_store_audit.py \
  --trace /private/tmp/nashura-loading-controlled-20261028-99okjE/frida-events.jsonl \
  --trace-sha256 c941e31ed0cc915b241cd789d670dab38e27199822efad5bc1947a2c20938b58 \
  --output /private/tmp/nashura-loading-controlled-20261028-99okjE/disk-audit-strict.json
# exit 0；输出已存在，审计器禁止覆盖；再次离线检查应换一个新输出路径
```

事务日志 SHA 为 `c941e31ed0cc915b241cd789d670dab38e27199822efad5bc1947a2c20938b58`，
严格审计输出 SHA 为 `f4e6672ca1f9fe49d0aaa8236376889529c200d6fb4475914c2fc35c9b3a598e`。
私有原始文件未纳入发布；公开 JSON 保存脱敏审计结果、实际检查命令和输出身份。

独立源码副本中的专项回归为 `577 passed, 1 skipped`，exit 0；
主工程为 `211 passed`，exit 0；公开九组为 `1,104 passed, 42 skipped`，exit 0。
检查均在新源码副本中运行，原工作树250份XML哈希保持不变（含历史和输入XML）。
公开回归和发布检查见机器证据及回执。
所有测试集合分别报告，不累加；工程测试不代表原生生命周期通过。

## 未闭合项与下一步

| 项目 | 状态 |
|---|---|
| 一次真实写入/恢复、四次同步 commit、双槽字节恢复 | RUNTIME_OBSERVED |
| fsync 系统调用及耐久性 | UNCOVERED |
| Backup journal | NOT_OBSERVED_IN_THIS_WINDOW |
| 真实异步写盘归因、跨进程重启恢复 | NOT_RUN |
| 当前 codec 完整输入域、异常及初始化身份 | UNRESOLVED |
| Linux Backup binding | UNRESOLVED |
| 严格 CATALOG_READY_PASS | BLOCKED |
| ResourceManager、GameState、真实 GameStateStep、完整局 | NOT_RUN |
| no-VM、Android-free、训练、10× | NOT_RUN |

下一道阻塞是完整配置后端的 runtime codec/storage/Backup 契约，尤其 Linux Backup
绑定仍显式失败。优先使用现有 DEX 和归档做只读闭合；真实写入授权已消费，
本轮不重复 store。契约通过后才在新 libg 子进程验证自然 catalog，保持原初始化顺序；
严格门通过后再推进战斗对象图。
