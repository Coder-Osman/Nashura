# E2.1-N：Backup 自然分支观察与 journal/scheduler 契约

基线：`f756ba02eed7d684399d4e4bcc2084e642bf5514`；工作分支
`codex/pure-linux-libg-headless-20261026`。本阶段为原配置依赖调查的继续，
`CATALOG_READY_PASS=false`，没有进入 ResourceManager/GameState/Step。
本报告与机器证据由包含它们的提交标识；不建立自引用提交 SHA。

## 真实结果：NOT_OBSERVED

在原隔离 Android 30 arm64-v8a AVD `emulator-5580` 的 system_server PID522 上，
安装六个被动 hook，观察45.006588917秒。目标 dataChangedImpl 入口0次，事件为空，
丢事件0、活动scope0、观察器错误0；正常detach。进程boot_id/starttime在附加前、
安装后及观察后核对，前后starttime为1230。游戏进程只读复核仍为PID6901。

**NOT_OBSERVED 只表示这个窗口没有目标自然请求**，不证明Backup依赖不存在，
不识别历史两次事务的put返回值，也不补足journal持久化或job交付证据。
没有重新store、主动请求Backup、清pending、重启游戏或重新加载Linux libg。
用户之前批准的受控写入/恢复及一次主动请求保持已完成状态，本阶段没有再次消费。

探针仅提供install和snapshot RPC。在目标入口的同线程scope内，以真正的
java.util.IdentityHashMap匹配mPendingBackups对象身份，捕获原HashMap.put返回是否null，
并关联journal及两个schedule重载。原方法恰好调用一次，原返回/异常继续传播；嵌套scope
退出后恢复父scope。事件有界，观察错误和活动scope显式使覆盖不完整。
Runner锁定探针SHA、精确六hook集合、进程身份；输出独占0600，失败留档并detach。
这里安装成功不等于命中过真实分支；本轮也没有做插桩前后的战斗轨迹等价验证。

## 新的静态发现：返回不等于副作用完成

复用同一实例合法提取的services DEX及原始dexdump，逐条审阅六个方法、158条指令
及catch table。新增工具把人工审阅结论绑定到规范化方法SHA；工具做身份绑定，
不做自动语义证明。以下全部为 **STATIC_FOUND**，不是本轮运行覆盖。

| 方法 | 精确静态语义 | 尚未验证 |
|---|---|---|
| DataChangedJournal构造 | Object构造后保存File到mFile | 文件底层实现 |
| newJournal | 给定目录中createTempFile，前缀journal、suffix为null，再包装File | 临时文件及异常的传递实现 |
| addPackage | RandomAccessFile的rws模式，length→seek末尾→writeUTF→close | 实际写入、fsync、崩溃持久化 |
| writeToJournalLocked | mJournal为空时创建；调addPackage；IOException被记录后清空mJournal并正常返回 | 真实IOException覆盖及journal内容 |
| schedule四参重载 | class monitor内按user去重；已scheduled则直接返回；否则构造并提交JobInfo | 本轮到底走哪个分支、任务接受及交付 |
| schedule三参重载 | 以delay=0转调四参重载 | 实际下游分支 |

addPackage的保护区异常会尝试close，二次close异常addSuppressed，再重抛原异常。
writeToJournalLocked仅捕获IOException；其他异常不由该handler吞掉。
因此，仅看到service journal方法正常返回不足以认定落盘成功。

schedule读取interval、fuzz、network及charging；正delay直接使用，否则interval加
Random.nextInt((int)fuzz)。JobInfo带minimum latency、网络、充电约束、86400000ms
deadline及userId extras。JobScheduler.schedule的int结果被忽略；正常返回后更新
per-user scheduled标记及wall-clock next time。**进入/返回schedule不足以证明job接受或交付。**
RandomAccessFile、File、JobScheduler等传递依赖没有因此闭合。

## 实际命令与输出

工作目录均为Nashura；原始输出保留在本机新私有目录，复跑须使用新的输出名：

```sh
/tmp/nashura-frida-16.7.19-venv/bin/python tools/pure_linux/run_backup_branch_passive.py \
  --adb /Users/gujiga/Library/Android/sdk/platform-tools/adb \
  --serial emulator-5580 --pid 522 \
  --script tools/pure_linux/backup_branch_observer.js \
  --script-sha256 1551ceb9d8a472e52428f6fb7acb33b5f336d626e7cde61c9541928e5913888e \
  --seconds 45 \
  --output /private/tmp/nashura-backup-passive-20261028-Xy7rrt/passive-first.json

/opt/anaconda3/bin/python3 tools/pure_linux/backup_journal_contract.py \
  --dump /private/tmp/nashura-backup-queue-20261028-8KeLJO/static-contract/dexdump.stdout \
  --dump-sha256 88b8396fea3c5f786aee3f44b6dba41767d0ccbe2a65839ada57c6708adc3e4a \
  --dex /private/tmp/nashura-backup-queue-20261028-8KeLJO/services.dex \
  --dex-sha256 66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a \
  --output /private/tmp/nashura-backup-passive-20261028-Xy7rrt/journal-contract.json

/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider \
  tools/pure_linux/test_backup_branch_passive.py \
  tools/pure_linux/test_backup_journal_contract.py
```

三项最终结果均exit0；43项新增测试通过。Node mock验证首次/重复put、对象身份、
无关键和线程、嵌套scope、异常、观察失败和容量边界；fake Frida验证被动RPC、
独占输出、身份/SHA/覆盖校验及detach。合成DEX测试验证精确方法绑定拒收边界。
这些测试仅验证工具，**不代替真实Backup副作用或引擎语义**。

全量专项、主工程及公开回归使用独立源码副本，日志、退出码、输入与输出SHA见
同阶段机器证据及回执。公开副本只含脱敏摘要，不含DEX、原始汇编、XML或私有事件。
本轮没有重新采集完整引擎/资源身份，不声称完成引擎前后身份门。

## 首个阻塞与下一项最小实验

首个未闭合依赖仍是loading配置的真实Backup副作用及其Linux绑定；Linux候选
preference_store_hook中的unresolved_backup仍显式失败，没有改成统一成功。
历史queue分支、持久化/交付、完整配置异常域及跨进程恢复分别保留UNRESOLVED/NOT_RUN。

下一项独立工作应沿已锁定的File/RandomAccessFile和JobScheduler传递调用审阅
实际写盘与提交契约，形成可验证的最小Linux回调，再以新libg子进程自然初始化catalog。
若需要新的受控写入或主动Backup，需另有明确实验授权；本轮未重复触发。
只有严格取得CATALOG_READY_PASS后，才继续ResourceManager与GameState对象图。
本阶段没有no-VM、Android-free、完整对局或10×证据。
