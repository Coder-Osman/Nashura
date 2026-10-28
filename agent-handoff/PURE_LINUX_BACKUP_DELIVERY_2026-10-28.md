# E2.1-N：真实 Backup JobService 回调与交付边界

日期：2026-10-28。工作基线 `a43620d533e433127057d947347e2238931813e7`；
仅推进 `codex/pure-linux-libg-headless-20261026`。提交身份以承载本报告的 commit 为准。

## 结论与验收边界

本轮取得 **RUNTIME_OBSERVED_LIMITED**：专用 Android ARM64 实例中的自有测试应用
发起一次 Backup 请求，真实 JobScheduler 在一次受控强制执行后进入
`onStartJob → backupNow → cancel`，三者原方法正常返回。
这是提前执行已经观察到的真实 job，**不是自然到期**。

工作线程日志显示目标 agent 被调用、pass 结束、数据量 **0 bytes**。
没有证据证明非空 transport 上传；没有完整工作线程插桩、journal 删除计数或
跨线程强因果 token。Linux Backup 绑定仍 **UNRESOLVED**；
严格 `CATALOG_READY_PASS=false`，不开放后续原生对象图。
本轮没有重载 libg，也没有重跑自然 catalog 初始化。

用户授权的一次真实游戏 store 与原值恢复在前阶段已经完成，本轮没有重复。
本轮自有应用一次请求与一次真实 job 强制执行分别归档；准备期 bmgr run
和其他系统包工作不混入目标请求计数。未修改原 APK、libg、资源、Provider 或更新文件。

## 实现

新增五个源文件：

- `backup_delivery_observer.js`：四个 Java 方法 hook，同线程父子 scope、序号、单调时钟、
  4096事件上限；原方法调用一次，原异常重抛，finally 恢复 scope。
- `run_backup_delivery.py`：专用 AVD、无游戏安装、APK/platform SHA、
  system_server PID/boot/starttime/cmdline 和 observer SHA 门禁；
  独占0700目录、归档命令输出、0600 result，不重试。普通输出由父目录限制访问。
- `backup_delivery_audit.py`：SHA、命令归档、实验边界、父子链、原返回、
  实际 job/user、pending/scheduled 和次数严格审计；拒收回调结束后的无关调用。
- `backup_delivery_scope.cjs` 与 `test_backup_delivery.py`：
  五种 Java mock 控制流、篡改拒收、独占文件、runner 失败不重试等48项工程检查。

observer 发布字节与真实运行时归档相同，SHA 为
`6260cc61c890ec6bd723a30b6d581b4a612bf6e29730126fb91cebc2a52b87a8`。
审计器后来收紧边界并对原始证据重新离线运行，未追加 Android 请求。
mock 和审计器测试不代替原生战斗或 transport 语义。

## 环境与固定身份

专用实例 `nashura-e2-backup-contract-api30 / emulator-5582`，ARM64 Android 11，
Frida 16.7.19。完整 fingerprint、输入/输出 SHA、实际命令归档身份见
[机器证据](pure-linux-backup-delivery-evidence-20261028.json)。

自有 fixture APK SHA：
`d6286d14c826c74d53f4ad416ef7112b57109925f2fd19bf4d376b70f1c1208a`；
services.jar：
`c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
结束前重新读取并核验两者 SHA 未变。专用实例没有安装游戏。
本轮没有重新核验历史真实引擎全套身份，不把历史 SHA 当作本轮加载证据。

## 首次失败完整保留

v1 的11-hook观察器在准备期 `bmgr run` 时观察到所附加的 system_server 崩溃；
runner exit1，目标请求数0。系统自动恢复，PID471变为4840。
崩溃线程 `key-value-backu`、SEGV_ACCERR，PC位于该线程 stack_and_tls 映射。
具体原因 **UNKNOWN**，未归因为某个 hook、栈溢出或游戏引擎故障。

原观察器、result、退出码和崩溃日志保持私有原字节；公开仅 SHA。
v2 缩小到四hook后成功，工作线程仍标为
`UNINSTRUMENTED_AFTER_OBSERVER_CRASH`，没有把缺口写成零调用。
Frida daemon 初始外层等待超时、后续实际存活，setup.json 仅存前五命令；
不将该准备归档宣称为完整成功。一次只读 DEX 检查误对 str 调用 decode，
exit1，修正后 exit0，未影响动态进程或证据。

## v2 实际观察

13条 ADB 命令全部 exit0，runner exit0；
system_server PID4840、starttime71416、boot_id前后一致。
共10事件，其中准备4、实验6；丢事件、观察异常、活动scope均0。

真实排队 job `52417896`、user0，最小延迟14,416,727ms。
目标 pending 从准备后false到请求后true，工作窗口结束后false。

| 同线程4840的实验顺序 | 观察 |
|---|---|
| onStartJob enter，call3 | scheduled=true |
| backupNow enter，call4，parent3 | pending=true，scheduled=false |
| cancel enter，call5，parent4 | scheduled=false |
| cancel return | 原方法正常返回 |
| backupNow return | pending仍true，jobs为空 |
| onStartJob return | 返回false，scheduled=false |

`clearScheduledForUserId` hook入口0，但scheduled确实变化；
直接调用覆盖 **UNKNOWN**，可能存在内联等原因，未确认。
不能由零hook事件推导依赖不存在。onStartJob返回false也不代表异步Backup完成。

随后日志中 PID4840/TID9519 出现目标包 Starting、agentConnected、Invoking agent、
fixture helper、backup_package(target,0)、K/V pass finished。
此为日志层面关联，没有跨线程强因果 token。最终又观察到scheduled=true、
新延迟14,659,337ms；窗口中其他系统agent失败重排，未归因为目标请求重复或失败。
dumpsys 中40bytes state和Ever backed up可能准备期已经存在，不归因为本次新增。

## 静态方法边界

对既有真实 services.dex/dump只读选择四方法，参数/寄存器、指令及catch表规范化SHA
由现有审计器生成；分别35、10、63、106条指令，完整SHA见机器证据。
私有 dump 不上传，静态解释均标记 **STATIC_FOUND**：

- `transportPerformBackup` 在 file.length≤0 时报告空数据并立即返回0；
  此分支在连接transport、performBackup和finishBackup之前返回。
- 非空分支打开descriptor、连接transport、执行performBackup；
  status0才finishBackup，异常包装和关闭副作用保持存在。
- `sendDataToTransport` 检查backupData并处理widget，
  根据savedState长度选择nonincremental；PackageInfo重载包装IOException。
- `finishTask` 涉及剩余队列重排、journal删除、restore token、
  backupRunning清理、transport初始化及可能的full-backup后续任务。

这些静态分支解释了为什么0bytes日志不足以证明transport上传，
**不作为动态 transport 计数**。传递被调用者与异常完整域没有闭合。

## 验证与退出码

```sh
python -m pytest -q -p no:cacheprovider tools/pure_linux/test_backup_delivery.py
node --check tools/pure_linux/backup_delivery_observer.js
node --check tools/pure_linux/backup_delivery_scope.cjs
python tools/pure_linux/backup_delivery_audit.py \
  --input "$PRIVATE_RUN/observe-v2/result.json" \
  --input-sha256 e9079d7ff63d8b55cdf4b0eb93a9824f1f3ae2568bf53406a1b9325e2b43c0a6 \
  --output "$PRIVATE_RUN/observe-v2/audit-v2.json"
```

48 passed，语法及真实离线审计均exit0；13份命令stdout/stderr与observer归档SHA核验。
实际 runtime argv和退出码归档在私有目录，公开证据提供精确SHA；
上面的变量仅脱敏显示路径，不是另一次真实触发。

完整回归在独立源码副本执行，五个源文件字节与提交候选相同；
原工作树251份XML保持原SHA。专项773通过/1跳过、主工程211通过、
公开九组1104通过/42跳过，三组均exit0；精确命令、日志SHA和耗时见机器证据。
各集合独立，不累计为额外测试数。
发布前源码清单2539项核验、gitleaks凭据扫描及git diff检查均exit0；
命令记录见机器证据，公开副本不包含原始trace或第三方二进制。

## 资源释放与下一步

结束前核验专用AVD名、host PID16708启动命令、无游戏安装和输入SHA，
仅对emulator-5582执行emu kill，exit0；随后PID16708不存在、ADB仅剩受保护5580。
没有操作游戏AVD PID78756、Ubuntu VM、冻结任务或训练。

当前唯一前置阻塞仍是 **Linux Backup副作用契约及所有权尚未接入**：
工作线程完成、journal生命周期与失败分支待验证。原
`preference_store_hook.c::unresolved_backup` 保持失败关闭；
没有统一成功返回、伪JavaVM或手写ready。
下一步在缩小的独立实验中补齐工作线程完成/清理与失败路径证据，
再定义Linux pending/journal/scheduler的明确所有权和交付契约；
严格catalog通过后才进入ResourceManager、GameState和真实Step。
