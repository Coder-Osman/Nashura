# E2.1-N：catalog 自然 ready、配置依赖纠错与真实 update 路径

分支 `codex/pure-linux-libg-headless-20261026`，源基线
`4b581dd0703f938a86a5ab88aa8722c450858f96`。提交身份以包含本报告的提交为准。
逐次命令、退出码、输入/输出及私有日志 SHA 见
[机器证据](pure-linux-catalog-config-evidence-20261028.json)。路径替换为占位符，
公开内容不包含二进制、资源、反汇编、原始内存、寄存器、私有路径或凭据。

## 结论与门禁

真实 Ubuntu ARM64 的两个新子进程 v78/v79 均 wrapper0、child0。
原生 loader 执行53步、到终态9，ready 自然0→1；162张表、24,353行，
布局摘要一致。**这是使用单键 native 配置候选的 RUNTIME_OBSERVED，
CATALOG_READY_PASS 仍为 false，整体 BLOCKED。**

首个未解决契约是 `LOADING_SCREEN_STATE_LABEL` 的配置存储语义，
不是缺少 APK，也不是已证明 BackupManager 对战斗必需。
加密、commit/failure 和 Android backup 外部副作用尚未闭合；
表布局、非空行指针及 ready 不代表 cell 内容或 Android 状态等价。
ResourceManager 仅观察到对象/工厂槽和资源加载路径，整体门未过。
GameState、空场、reset/destroy、真实 Step、动作、多tick、快照、完整局及Android逐tick对照
均 NOT_RUN。未进行训练、吞吐或10×实验，未取得 no-JavaVM 冷启动或 Android-free 结论。

## 修正旧归因，不覆盖旧证据

v59的版本绑定post-load断点实际顺序是：loader_start → PreferenceWrite →
raw store bridge → ApplicationUtilBase.storeKeyValue → JNIEnv acquire →
空JavaVM虚表读取SIGSEGV。键为 `LOADING_SCREEN_STATE_LABEL`，值为 `datatables-init`。
AttachCurrentThread尚未执行。旧报告将其写成日志桥，现予纠正。
原三个Provider/catalog报告及机器JSON原字节保留，故障与BLOCKED记录继续有效。
本次追踪不包含constructor覆盖，回溯有不完整unwind；其他断点零命中不代表依赖不存在。

当前输入的DEX静态证据显示 p优先、p缺失时从o修复、写入/修复触发requestBackup，
remove进入store(null)，另有加密及SharedPreferences.commit。
这些标为 STATIC_FOUND，不冒充运行时 Android 行为。当前DEX与历史DEX SHA不同，
沿用本次APK身份，不把它包装为历史锁定整包。

## 新实现与候选边界

新增有界字符串观测、已解析表布局诊断、资源syscall解析、单键持久化候选及跨进程探针，
扩展版本入口守门、runner和测试。代码位于 `tools/pure_linux/`。

配置候选只接收实际观测的键及4095字节以内ASCII值；未知键/坏状态显式失败。
实现p/o、读修复、原子rename、fsync、恢复及损坏/symlink/pending拒绝。
backup_requests是本地计数，**不是** BackupManager.dataChanged。
原始libg文件未变；候选仅在新子进程中、入口字节校验后重定向一个raw write bridge，
临时RW后恢复RX并清指令缓存。未替换read/remove/cache/lock路径，未写ready，未伪造VM。
候选标记固定为 `isolated_native_candidate_no_android_backup_parity`，严格门保持false。
尚未验证并发多进程写入，因此仅用于独立、单写者实验。

v77真实ARM64/Bionic六个独立进程依次验证fresh写、restore、未知键拒绝、
已有状态fresh拒绝(exit3)、真实loader状态副本restore、该副本未知键拒绝。
其他五个harness exit0，所有预期退出码匹配，源state与probe未变。
状态文件8216字节，两个loader结果及restore源SHA一致。
这只证明native候选的限定行为，非Android语义验收。

## 真实资源与自然加载

沿用真实libg、8游戏库、9,829个APK资源身份。实验暂存619个APK表文件
8,043,281 bytes、990个真实update文件151,493,511 bytes；逐次前后manifest、
probe和辅助输入SHA不变。仍使用匹配Android linker/Bionic；未启动Emulator/ART/UI/EGL。
guest记录为aarch64、Linux6.8.0-138、glibc2.35，v78前磁盘1,298,956,288 bytes；
仅执行有界小实验，未清理历史文件。

v70/71/72通过真实原生资源入口分别读取显式update、APK search-root、update search-root。
同名文件的APK/update字节SHA不同，dump完整SHA、length/read/EOF/seek和close通过，
child/wrapper均0。该资源矩阵仅是实际同名路径，不泛称所有fallback边界通过。

APK-only v67–69自然ready，162表/23,868行；真实update作为search-root的
v75/76及v78/79均为162表/24,353行。新profile实际377次preload请求；
默认368仍保留，使用显式 `--expected-preload-requests 377`，不放宽语义门。
v78/v79布局SHA均为 `8ff1043b9d3544dbd6cbefae576025845e1f5f6e8cb9a78a931428eb5de36f89`。

两次新追踪均完整exit0、无截断行、未知相对路径0、末尾资源fd0：

| 观察项（每次） | APK | update |
|---|---:|---:|
| 成功open/唯一路径 | 247/247 | 131/131 |
| 文件长度合计 | 3,421,119 | 2,805,535 |
| read调用/实际返回字节 | 247/3,421,119 | 0/0 |
| 完整只读mmap | 0 | 131 |
| close | 247 | 131 |
| 已观察错误/越界 | 0/0 | 0/0 |

1,147次update exists中131成功、1,016次ENOENT（508唯一路径），无其他错误。
按单进程trace行序核验，247次APK打开前均有同路径update ENOENT，
131次update打开前均有同路径exists成功；378个打开路径在对应前后manifest中SHA相同。
这是实际路径的有序关联，非任意namespace优先级或异常资源全覆盖。
mmap只证明映射范围，不证明CPU逐字节消费；syscall trace不校验内存字节SHA。
独立资源dump才用于字节校验，不把 `resource_access_verified=false` 改写成true。

## 失败及未知保留

- v59：真实SIGSEGV；GDB/inferior的null退出码与信号分开记录。
- v64–66：probe输出finish0，但strace达32MiB限额，wrapper=-25/SIGXFSZ，终止记录不全。
  后续限定资源syscall并将read参数raw化，未扩大日志预算掩盖失败。
- v73/74：child0，旧validator因368与377差异wrapper1；另起目录显式绑定profile后通过观察。
- v75/76：wrapper0但旧parser遗漏Bionic目录dup，部分路径unknown；v78/79补追踪并解析，旧结果保留。
- v70–72：未启config候选，选取不存在config状态的可选传输exit255；资源实验仍通过。
- update初次staging helper语法错误exit1，独立retry0；旧记录保留。

## 工程验证与命令

在独立源码副本执行（本地绝对路径已脱敏）：

```sh
python3 -m pytest -q -p no:cacheprovider tools/pure_linux
python3 tools/run_main_engine_checks.py
python3 tools/run_public_checks.py
```

分别exit0：专项157通过/1个Linux条件跳过；主工程211通过；公开9组XML零失败零错误。
专项在补充trace行序诊断后再跑仍157/1。历史XML留在原仓库，测试副本及原始输出私有保存。
测试数量按各范围记录，不累加，不替代原生生命周期。

实际原生命令的完整参数及wrapper/child退出码、输入输出SHA在机器JSON中；
`<PAYLOAD>`等仅替换私有路径。核心增量参数为：

```sh
python3 tools/pure_linux/provider_job.py <identity-and-resource-arguments> \
  --mode both --data-path / --search-root <REAL_UPDATE> --factory-mode register \
  --catalog-mode loader --loading-config-candidate --inspect-parsed-tables \
  --expected-preload-requests 377 --resource-trace --timeout 55
```

## 下一步唯一前置阻塞

先闭合单键配置的原Java契约、编码和失败/持久化/外部副作用边界，
利用已合法准备的同版本代码及既有Android证据；未被覆盖的副作用继续UNRESOLVED。
严格catalog门通过后再验ResourceManager完整初始化/资源取用/生命周期；
两道门均通过后才允许进入GameState。当前不以自然ready或单元测试越过该顺序。
