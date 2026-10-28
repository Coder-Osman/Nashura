# E2.1-N：真实 Backup agent 失败、重排与重试

基线 `5d1a34679e36207568190a583e316201d51558f6`；本报告属于
`codex/pure-linux-libg-headless-20261026`，交付SHA为承载本报告的commit。
历史失败、BLOCKED和既有证据保留。结果为 **RUNTIME_OBSERVED_LIMITED**。

## 本轮完成的闭环

在专用 Android 30 ARM64 AVD 中安装全新自有包 `org.nashura.backupretry`。
没有安装游戏，没有重复真实游戏store；之前一次真实游戏store/恢复授权已使用。
Receiver只提交一次非空SharedPreferences和一次真实dataChanged；agent首次消耗
独立no_backup标记并抛出真实IOException，第二次调用平台SharedPreferencesBackupHelper。
保留Java VM、原平台服务与LocalTransport，没有hook或替换system_server。

执行顺序为预备一次`bmgr run`，静止快照，store/request，入队快照，
一次`bmgr run`及失败快照，再一次`bmgr run`及成功快照。
没有backupnow，没有追加第二次请求。两次消费由实验提前触发，非自然job到期。

| 证据 | before | queued | after_failure | after_retry |
| --- | --- | --- | --- | --- |
| 全服务pending数 | 4 | 5 | 3 | 2 |
| 目标包pending | 否 | 是 | 是 | 否 |
| 当前journal目标条目数 | 0 | 1 | 1 | 0 |
| 目标XML | 明确诊断缺失 | 215 bytes | 完全相同 | 完全相同 |
| 目标transport | 明确诊断缺失 | 明确诊断缺失 | 明确诊断缺失 | 231 bytes |
| 目标state | 明确诊断缺失 | 明确诊断缺失 | 0 bytes | 72 bytes |

17:38:44.541记录store/request正常返回。17:38:45.323–.324，agent在线程5484
进入并抛出IOException；服务线程5535记录backup_agent_failure及本次pass结束。
17:38:53.652，agent在线程5483第二次进入并返回；服务线程5715记录259bytes流和pass结束。
agent两次PID相同5466。系统进程PID475的boot/starttime在实验前后保持一致。
这些是日志窗口归属，并非方法hook计数或所有异步调用的完整因果证明。

失败后原目标journal退出列表，新journal含目标条目；成功后后者也退出列表。
存活旧journal字节不变；首次请求的journal为原字节后精确追加一条目标包。
journal退出目录列表是运行观察，`DataChangedJournal.delete()`实际调用次数仍为UNKNOWN。
成功transport偏移16后逐字节等于215bytes XML；16bytes头语义仍为UNKNOWN。

## 静态分支与证据边界

审阅匹配services.dex中的`KeyValueBackupTask.run/finishTask/revertTask`：
run的AgentException分支仅isTransitory为真时重排；TaskException走另外的reset/revert路径。
finishTask处理剩余queue、journal删除和收尾；revertTask读取transport延迟并调度原queue。
这是 **STATIC_FOUND**；本轮真实IOException、pending和新journal与重排分支一致，
但没有动态计数证明isTransitory、delete或每个transitive callee的实际调用。
TaskException、崩溃恢复、自然job到期和断电持久化均为 **NOT_RUN**。

## 采集缺陷与修正

实际实验使用runner v1，SHA为
`92f5791fd45e7b4b5a313bc804e26fc1a32b4899845bf832c77c59481077a3ee`。
`adb exec-out cat`在本环境缺失路径返回host exit0，错误文本落stdout。
因此历史缺失仅认定为精确cat诊断报告缺失，exit0绝不当文件成功。
只读followup使用`adb shell -T cat`，8条命令exit为`0,0,0,0,1,0,0,0`：
marker读取明确远端exit1，XML/transport/state与归档逐字节相同。
followup仅核对AVD、无游戏及系统PID，没有单独取得完整boot/starttime；
主实验本身已核对完整身份，关闭前另核验完整身份一致。这个覆盖差别保留。

公开runner v2改用`adb shell -T cat`，并在任何设备命令之前拒绝非5582 serial。
v2通过合成命令回放测试；本轮没有重新执行真实store，不把v2冒作历史实跑代码。
严格离线审计同时支持明确限定的v1诊断与v2远端状态，核对命令顺序、SHA、
文件、日志、线程窗口、journal精确追加和只读followup。73份命令归档审计exit0。
初次负例同时改写了enter/return线程，仍是合法轨迹，测试失败已保留；修正为只改enter线程。

## 实际命令与验证

命令中的`PRIVATE_RUN`代表机器证据记录的私有目录。token及签名口令不公开。
构建实际使用JDK25 javac、Android35 android.jar、build-tools36的d8/aapt/apksigner，
target/min API30；构建、签名和验证7条命令均exit0。确切argv保存在私有build/commands.json。

```sh
python tools/pure_linux/run_backup_retry.py --adb "$ADB" --serial emulator-5582 \
  --apk PRIVATE_RUN/build/fixture.apk --apk-sha256 "$APK_SHA" \
  --platform-sha256 "$PLATFORM_SHA" --token "$TOKEN" --output PRIVATE_RUN/observe
python tools/pure_linux/backup_retry_audit.py \
  --run PRIVATE_RUN/observe/result.json --run-sha256 4044bead361397deea6a66521f4b565300b5cc3dd08e2569de38328f1fad8c54 \
  --followup PRIVATE_RUN/file-followup/result.json --followup-sha256 187d8b780dced68b8f5a9cfaadfe8c5cefc78e947b525062bac551271b2f96e0 \
  --output PRIVATE_RUN/audit-03.json
python -m pytest -q -p no:cacheprovider tools/pure_linux
python tools/run_main_engine_checks.py
python tools/run_public_checks.py
```

实跑collector为上述命令对应的私有v1副本；现公开同名入口已为v2。
离线audit-03 exit0；采集65条子命令host exit均0，外层退出码未单独归档。
新增47项工具测试，专项857通过/1跳过，主工程211通过，公开1104通过/42跳过；均exit0。
253份原XML的SHA保持一致；6份待发布源码与独立测试副本逐字节一致。
公开回归、发布检查、源码SHA与完整输出SHA见[机器证据](pure-linux-backup-retry-evidence-20261028.json)。
所有回归在独立源码副本执行，工程测试与真实Android观察分开记录，计数不跨集合相加。
专用5582核验host PID/启动时间、AVD名及系统身份后关闭；5580、Ubuntu和冻结任务保留。

## 当前门与下一步唯一阻塞

**Linux Backup副作用所有权及真实配置桥绑定仍UNRESOLVED**。
本轮补充了自有agent的一种真实失败重排路径，没有运行Linux libg/catalog。
严格CATALOG_READY_PASS仍为false，ResourceManager、GameState及GameStateStep为NOT_RUN。
没有无VM、Android-free、完整原生对局或10×结论。

下一项聚焦Linux端最小Backup服务的所有权、持久化队列和错误传播如何绑定当前loading key，
逐项使用现有codec/双槽/commit/journal证据；未覆盖项保持显式unresolved_backup。
完成绑定后才在新libg子进程重跑自然catalog，严格门通过后推进后续对象图。
不以扩大Android Backup覆盖率代替Linux绑定，也不以常量返回成功处理缺失契约。
