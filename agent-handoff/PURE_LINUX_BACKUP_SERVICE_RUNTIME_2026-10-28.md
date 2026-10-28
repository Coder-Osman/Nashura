# E2.1-N：真实 Backup 服务入队、journal 与调度分支

本阶段状态为 **RUNTIME_OBSERVED_LIMITED**。在新专用 Android 30 ARM64 AVD 上，
自有测试应用的首次请求真实进入 pending 队列并追加 journal，第二次请求命中去重分支。
另捕获平台任务的实际 JobScheduler 提交；它来自独立平台调用作用域，未归因给这两次应用请求。
Linux Backup 副作用绑定仍未完成，**CATALOG_READY_PASS=false**。

## 身份与隔离

- 分支：`codex/pure-linux-libg-headless-20261026`；
  基线：`85ed2498d70aa7d3a3ed1cdfe79027710f45cb9e`。
- 交付提交：包含本报告的提交；实际完整 SHA 由 Git 和最终回执给出，避免自引用哈希。
- 宿主：Darwin arm64 / macOS 15.6 / 38,654,705,664 bytes 内存。
- 专用 AVD：`nashura-e2-backup-contract-api30`，`emulator-5582`，API30 ARM64。
  `system_server` PID486/starttime1170，boot ID
  `69ca10cb-fe2c-4b07-a429-26a64368bf94`，前后完全一致。
- fingerprint：
  `google/sdk_gphone_arm64/emulator_arm64:11/RSR1.240422.006/12134477:userdebug/dev-keys`。
- 自有应用 `org.nashura.backupfixture`，实际请求 UID10166。
  APK SHA：`d6286d14c826c74d53f4ad416ef7112b57109925f2fd19bf4d376b70f1c1208a`。
- 新实例未安装游戏；原游戏实例、Ubuntu VM、main 和冻结部署未操作。
  测试结束确认 AVD 归属与未安装游戏后，`adb -s emulator-5582 emu kill` exit0。
- 此轮未再执行游戏 store、恢复或游戏主动 Backup；历史一次授权操作已有独立证据。
  原始 trace、journal、测试 APK、签名材料保留在私有目录，不进入仓库。

## 实现与观测方法

新增自有 Receiver/BackupAgent、八 hook Frida 观察器、严格 AVD/进程/输入 SHA
绑定的 runner、独占 0600 输出审计器，以及36项独立工具测试。源码与 SHA 见机器证据。
原方法调用一次，返回值和异常继续传递；finally 清理同线程作用域。
通过 IdentityHashMap 匹配真实 pending HashMap，避免把任意 put 当成目标队列操作。

八 hook 覆盖 dataChanged、指定 dataChangedImpl、HashMap.put、
writeToJournalLocked、DataChangedJournal.addPackage、两个 schedule overload、
JobSchedulerImpl.schedule。完整列表在 JSON 中。事件上限4096；
丢事件、观察错误和活动作用域单独计数；零值只描述该观察窗口。

专用实例通过正常 LocalTransport、enable、bmgr run 清空目标 pending；
其他包的 scheduled 仍为 true。准备过程也被记录，不使用手工队列改写。
runner 明确发出两次应用请求，不自动重试。

## 实际运行结果

v6 runner exit0，独立审计 exit0；24事件、2个应用 entry、2个平台 scope；
dropped=0、observationErrors=0、activeScopes=0，11条 ADB 子命令全部 exit0。

| 路径 | 真实结果 | 分类 |
|---|---|---|
| 第一次应用请求 | pending false→true，put previousNull=true，writeToJournalLocked→addPackage | RUNTIME_OBSERVED |
| 第二次应用请求 | pending true→true，put previousNull=false，journal 0次 | RUNTIME_OBSERVED |
| 两次应用 schedule | scheduled true→true，未新增底层 job | RUNTIME_OBSERVED |
| 独立 platform-1 | KeyValueBackupTask.run→dataChangedImpl→schedule；false→true，submit返回1 | RUNTIME_OBSERVED |
| platform-2 | 无应用 scope 的平台 schedule，true→true | RUNTIME_OBSERVED |
| job 最终交付与 transport | 未追踪到交付 | UNKNOWN |
| Linux 服务绑定 / 自然 catalog | 本轮未运行 | UNRESOLVED / NOT_RUN |

platform-1 在线程7976提交 id52417896，minLatency=14,506,750ms，
maxDelay=86,400,000ms，要求 charging 以及
INTERNET/NOT_RESTRICTED/TRUSTED/VALIDATED 网络能力。
这些是本次实际 JobInfo 字段；不把随机延迟写成固定配置。
原始调用栈私有保存。应用 Binder→异步 handler 仅有同窗口顺序，没有强因果 token，
报告不把该独立平台提交写成应用请求触发。

v6 journal 共95bytes，三条长度边界完整，目标应用 ASCII 精确出现一次；
SHA `8a1e572961b01639d4daabdf50df9e42ad5b40e83474e0248ef0467c35fdf6fa`。
这里只验证大端两字节长度边界及目标字节，不声称完整 modified UTF-8 解码验证。

## 命令、输出与归档

私有根：`/private/tmp/nashura-backup-service-runtime-20261028-D5BDqv5a`。所有实际 ADB 参数、返回码与输出 SHA、
构建命令（测试密码脱敏）、源码快照 SHA、审计命令详见
[pure-linux-backup-service-runtime-evidence-20261028.json](pure-linux-backup-service-runtime-evidence-20261028.json)。
runner 等价重建命令也在 JSON 中；原外层 shell/wrapper argv 未归档，明确保留该缺口。

实际离线审计：

```sh
/Library/Developer/CommandLineTools/usr/bin/python3 tools/pure_linux/backup_fixture_audit.py \
  --input /private/tmp/nashura-backup-service-runtime-20261028-D5BDqv5a/observe-v6/result.json \
  --input-sha256 7d1b4ccafb67880d2a9b2130f9c873b356cb92d60f29203e428c787c624c6d15 \
  --output /private/tmp/nashura-backup-service-runtime-20261028-D5BDqv5a/observe-v6/audit-v2.json
```

exit0；审计 SHA：
`fff35ada5bd774e675cc8a4c607af16c7bebc8edddf7492ff1788819a897f71c`。
输出路径独占创建；重验应选择新的输出文件。

JDK25、build-tools36.0.0、android-35.jar、min-api30，构建七命令全部 exit0。
签名仅用于自有测试应用；keystore、APK 和含密码原日志私有留存。

## 失败记录与工具验证

| 尝试 | exit | 显式应用请求 | 结果与原因 |
|---|---:|---:|---|
| v1 | 1 | 0 | 安装状态命令处理错误 |
| v2 | 1 | 0 | 安装成功后对 incremental 输出识别错误 |
| v3 | 0 | 2 | 两次都是已有 pending；不满足首次入队覆盖 |
| v4 | 1 | 0 | 要求 pending=false 且 scheduled=false 过严；保留自然重入队 |
| v5 | 1 | 1 | 瞬时活动 scope 导致过早 entry 计数拒收；窗口不完整 |
| v6 | 0 | 2 | 最终静止快照与独立审计通过 |

v1–v4完整观察器源码快照归档有缺口，版本 SHA 与失败输出保留；
v6 runner/observer 源码快照与当前提交字节一致。
失败退出码来自实际执行记录，不从最终 shell wrapper 的 exit0 推断。

初轮误用系统 Python3.9：专项16失败/709通过/1跳过，主工程8 collection errors，
公开20 collection errors；原因是缺 cryptography 及旧版本不支持 dataclass slots。
保留失败，无关产品代码未修改。

在独立源码副本，用已有 Python3.13.5 重验：

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py
/opt/anaconda3/bin/python3 tools/run_public_checks.py
```

分别 exit0：**725通过/1跳过、211通过、1104通过/42跳过**。集合重叠，不累加。
新增36项中含6个 Node mock 场景，验证原调用次数、异常、嵌套与线程作用域；
这是工程证据，不是游戏原生轨迹对照。248份 tracked XML 逐字节对 HEAD 未变。

## 门禁与下一步

本轮 libg 未加载，游戏资源与 runtime 未重新核验；
ResourceManager、GameState constructor/setup/load、空场、reset、真实 GameStateStep、
无 JavaVM 冷启动、完整战斗、训练与10×实验均 NOT_RUN。

唯一当前阻塞仍为 **Linux Backup 服务副作用绑定**：
pending 与 journal 的所有权，以及 scheduler 失败、生命周期和交付契约。
当前 `preference_store_hook.c::unresolved_backup()` 保持返回 -1；
不以成功桩替代真实副作用，也不把 JobScheduler 接受当作交付完成。
绑定闭合后才重跑真实 libg 自然 catalog 初始化；
严格 CATALOG_READY_PASS 前继续保留后续对象图门。

Git fetch 本轮40秒超时，远端新鲜度 UNVERIFIED；本地提交后的有界推送结果以实际命令回执为准。
