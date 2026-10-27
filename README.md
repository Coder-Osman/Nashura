# Nashura

## 2026-10-28 E2.1-N：Backup callback 服务端语义

历史客户端 ART trace 重分析观察到23次 `requestBackup` 正常返回；匹配 Android 30
服务端 DEX 的11个方法静态调用图显示权限/用户检查、handler入队、journal写入和
备份job调度路径。客户端 Binder 调用是同步事务。参数和返回值未捕获，服务端未运行，
所以真实交付及callback完整契约仍 **UNRESOLVED**；严格 catalog 门保持关闭，
ResourceManager BLOCKED，GameState与真实Step仍 NOT_RUN。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_SEMANTICS_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-backup-semantics-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_SEMANTICS.md)。

面向实时、部分可观测对战的 FAIR 搜索、自博弈与策略迭代研究工程。

本仓库保存工程源码、冻结规范、独立实现版本与阶段性验证成果。现阶段重点是基础正确性和迁移验收；正式训练尚未启动。

## 2026-10-28 E2.1-N：双槽持久化接入真实 libg

新增双槽 XML/string-map、历史 ASCII codec 与严格配置桥。独立后端两个 ARM64/Bionic
child 各 48 条参考一致；真实 libg 四 child 完成双槽 commit 后因 backup 未闭合主动 exit96。
独立 NDK 服务查询在返回前因 /dev/binder ENOENT 而 SIGABRT，服务状态仍未知。
专项 482 通过/1 跳过、主工程 211 通过、公开九组 1,104 通过/42 跳过；保留历史 XML。
严格 catalog 及 ResourceManager 仍 **BLOCKED**，GameState/真实 Step/完整局均 NOT_RUN。
当前身份、XML/异常完整域及 backup 契约尚未闭合；详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_BACKEND_2026-10-28.md)及
[机器证据](agent-handoff/pure-linux-preference-backend-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置文件原语与平台审计

新增真实文件分步写入、bak恢复与异常退出验证；两个新ARM64/Bionic子进程
各19组/444项通过，187输入未变。离线核验Android30平台10方法，恢复sync和bak错误语义。
隔离专项409通过/1跳过、主工程211通过、公开九组1,104通过/42跳过；保留248份历史XML。
本轮未加载libg；XML/map、当前身份及真实backup服务仍未闭合，严格catalog保持 **BLOCKED**，
GameState与真实Step仍NOT_RUN。工程测试与原生语义分开记录，详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_FILES_2026-10-28.md)和
[机器证据](agent-handoff/pure-linux-preference-files-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：历史异常恢复与store0字节对照

store1四步padding异常/恢复序列在两份历史原版参考上逐字节匹配；
找到原版历史身份采集记录，store0两份参考各两个ARM64/Bionic子进程、每child30条全匹配。
各187输入未变，本阶段未重新加载libg。XML解析内容恢复不等于整份原字节恢复。
隔离专项378通过/1跳过、主工程211通过、公开九组1,104通过/42跳过，三组分别记录。
当前身份、完整异常及storage/backup仍UNRESOLVED，严格catalog保持 **BLOCKED**，
GameState和真实Step仍NOT_RUN。详见[报告](agent-handoff/PURE_LINUX_PREFERENCE_SEQUENCE_2026-10-28.md)
和[机器证据](agent-handoff/pure-linux-preference-sequence-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：持久Cipher与历史正常路径对照

新增持久AES候选、SHA绑定的身份提取及私有参考工具；两个新ARM64/Bionic子进程
各通过30条历史原版ECB/CBC字节参考，187项输入未变，9个实际映射核验。
专项304通过/1跳过、主工程211通过、公开九组零失败错误；首次映射采集失败保留。
本阶段未重新加载libg。当前身份、异常全域及真实存储/backup仍未闭合，严格catalog
保持 **BLOCKED**，GameState及真实Step仍NOT_RUN。详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_CIPHER_2026-10-28.md)和
[回执](agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_CIPHER.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：有界初始化图与分步配置语义

真实DEX初始化有界图142节点/15分支相同；完整onCreate仍不同。
新增显式后端C候选，恢复双store、分步commit、异常及backup调用顺序。
当前/历史DEX各4,522例有界差分零差异，保留首次252例差异；
两个ARM64/Bionic候选子进程exit0，未加载libg，外部接口仍为合成边界。
真实密码、平台存储及backup契约仍UNRESOLVED，严格catalog门 **BLOCKED**，
GameState与真实Step保持NOT_RUN。详见[报告](agent-handoff/PURE_LINUX_PREFERENCE_SEMANTICS_2026-10-28.md)
及[回执](agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_SEMANTICS.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置候选并发保护与初始化身份审计

配置候选加入进程生命周期租约和线程互斥；两个独立ARM64/Bionic实测均通过，
各11条进程回执、187项输入SHA不变。8个配置方法静态摘要相同，但初始化方法不同；
数组载荷相同仍不代表初始化控制流、持久化及backup契约闭合。专项181通过/1条件跳过。
严格catalog门仍 **BLOCKED**，ResourceManager整体门未通过，GameState及真实Step保持NOT_RUN。
详见[本轮报告](agent-handoff/PURE_LINUX_CONFIG_CONTRACT_2026-10-28.md)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_CONFIG_CONTRACT.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：catalog 自然 ready 与配置依赖纠错

真实 Ubuntu ARM64 的两个新子进程均 exit0，原生 loader 自然推进53步，ready由0变1，
真实加载162张表/24,353行。两次追踪各确认247次update缺失后APK打开、131次update存在后打开。
此前空JavaVM故障已定位为配置写入桥；以下旧报告中的日志桥归因由新证据纠正，原文保留。
单键native持久化候选尚未闭合Android加密、commit及backup外部语义，严格catalog门仍
**BLOCKED**；ResourceManager整体门未通过，GameState及真实Step保持NOT_RUN。
专项157通过/1条件跳过，主工程211通过，公开回归exit0、9组XML零失败错误。
详见[新报告](agent-handoff/PURE_LINUX_CATALOG_CONFIG_2026-10-28.md)及
[机器证据](agent-handoff/pure-linux-catalog-config-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：Provider 注册及真实资源读取

Ubuntu ARM64中新子进程完成data/update原生注册和四次完整资源读取，字节SHA、
read/EOF/seek/close通过；catalog两次list158/preload368及分批调用返回，ready仍0。
自然loader在日志路径获取JNIEnv时读取空JavaVM指针而SIGSEGV，整体 **BLOCKED**。
108项专项通过、1项平台条件跳过；主工程211项通过，公开回归9组XML零失败。
GameState/真实Step及完整局未运行，未取得无JavaVM或Android-free结论。
详见[本轮报告](agent-handoff/PURE_LINUX_PROVIDER_CATALOG_2026-10-28.md)。以下保留历史状态。

## 2026-10-27 E2.1-N：真实constructor及内容对象构造

在真实Ubuntu ARM64 guest中，linker记录libg753次constructor进入/返回；
内容root/context和filesystem原生构造均返回exit0，187项输入前后SHA一致。
新增限定结果 **CONSTRUCTOR_TRACE_PASS / CONTENT_CONTEXT_PASS**；ready仍为0，
provider/catalog未接通，整体 **BLOCKED**。GameState、empty battle和真实Step均未运行。
限定JNI/Asset断点计数不代表全路径无依赖；实际观测到Android属性/logdw路径访问。
详见[本轮报告](agent-handoff/PURE_LINUX_NATIVE_INIT_2026-10-27.md)。以下保留历史状态。

## 2026-10-27 新输入续验：真实 libg 已取得 LOAD_ONLY

用户提供的同版本 APK 中，8 个 ARM64 库及全部 9,829 个游戏资源与既有基线逐项 SHA
一致。为该输入单独登记身份，保留历史整包 SHA 差异。在真实 Ubuntu ARM64 guest 中，
匹配 Android linker/Bionic 对真实 libg 执行 dlopen/dlclose，进程 exit0，取得 **LOAD_ONLY**。
全部 187 个暂存输入前后未变；未初始化战斗，未验证无 JavaVM 生命周期、完整对局或10×。
详见[报告](agent-handoff/PURE_LINUX_USER_APK_LOAD_2026-10-27.md)。以下保留历史状态。

## 2026-10-27 续验：ARM64 Linux 与 Bionic 合成 ABI 已实测

经用户授权恢复现有 Ubuntu ARM64 VM，真实 guest 的 Linux 八项 smoke、文件 canary 和
退出码通过，取得 **PURE_LINUX_SMOKE_PASS**；匹配 Android linker/Bionic 的线程/TLS 探针
也退出 0，已核对隔离目录中的 libc/libdl 实际绑定。新增版本符号/relocation 静态审计，
29 项专项通过。初次 clone 探针失败和修复后输出分别保留。

这些结果仅验证合成 ABI，原版 libg 尚未加载：锁定 APK 身份仍缺失，原生内容/manager
初始化依赖仍待验证。no-VM、Android-free、真实完整对局、10× 和训练均未启动。
详见[续验报告](agent-handoff/PURE_LINUX_GUEST_ABI_2026-10-27.md)及
[回执](agent-handoff/CODEX_REPLY_2026-10-27_PURE_LINUX_GUEST.md)。以下保留此前状态。

## 2026-10-27：Apple Silicon → ARM64 Linux 原版引擎审计

任务分支新增匹配 ELF/输入审计及 direct ARM64 Linux、Bionic 探针，15 项专项通过，
两种探针交叉构建成功。214 个运行文件、990 个资源匹配，静态检查 185 个 ARM64 库。
当前 **BLOCKED_ENVIRONMENT**：没有已验证运行中的独立 ARM64 Linux guest，既有挂起 VM 保持原状；
原始锁定 APK 另有身份缺口。guest smoke、真实 libg 加载/生命周期、无 VM 与 10× 均未验证。
详见 [审计回执](agent-handoff/CODEX_REPLY_2026-10-26_PURE_LINUX.md) 与
[ABI 实验](agent-handoff/ARM64_LINUX_ABI_FEASIBILITY.md)。以下保留此前阶段记录。

## 当前状态（2026-10-25）

| 阶段 | 状态 | 尚待完成 |
|---|---|---|
| G0/G1：工程与搜索正确性 | 进行中；限定子项登记 34/43 | 完整 Full128 搜索终局、原生补充轮次与选优、一般原生边界等 |
| G2：正式迁移与 WDL 校准 | 未运行 | 正式 1,280 局数据、校准与非劣验收 |
| G3：搜索增益 | 未运行 | 冻结迁移模型的 128 次搜索实战对照 |
| G4–G8：后续训练与发布门 | 未运行 | 依赖前置门闭合 |

测试通过、限定子项通过、整体门通过和模型实战提升分别记录，测试数量存在重叠，不累加。

新增整轮顺序执行与独立回收：固定准备结果和选优报告，逐局调用既有worker及
独立归档验收器；中断后重验已完成局而不重复采集，失败保留现场。联合117项
工程测试通过、主工程211项通过，集合不相加。尚未执行真实round2，重试调度
及训练交接待完成；G0/G1与34/43不增。Full128最新快照提交185组/370根/
47360模拟、tick5990继续，terminal仍运行。详见
[_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md](_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md)。
以下为此前阶段和时间点记录。

新增整轮补充采集协调预检：任务精确覆盖、输入与外部种子来源一致、输出隔离，
逐任务预检全部通过后才生成准备标记及现有worker命令。32项专项、128项联合
工程回归通过，集合不相加；仅准备，不执行worker或启动引擎。真实terminal、
原生round2及完整搜索局仍待验，G0/G1与34/43不增。首次公开专项因共享测试
依赖私有manifest失败，原报告保留；显式合成计划修复后源联合113项、公开专项
32项均通过且零跳过，完整公开入口通过。Full128快照1792916545.8936303提交
184组/368根/47104模拟、tick5982继续；terminal第18步完成、第19步开始。
详见[_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md](_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md)。
以下为此前阶段和时间点记录。

新增模型格点到原生执行坐标限定验收：62份归档独立重算，三组各426次决策/
20个观察边界及tick5897终局一致；188预约的转换、入队与原生加载坐标全部
一致，其中33个带位置偏移，所属清理通过。最终41项专项通过、零跳过，含
真实归档重验及3,456种冻结转换对照。内部卡牌、完整mask、一般拒绝与全部
随机覆盖仍待验。Full128快照1792915142提交183组/366根/46848模拟继续，
未终局；terminal第18步验证到192/358行。G0/G1及34/43保持开放。详见
[_spec_work/versions/fair-final-command-coordinates-v1/EVIDENCE.md](_spec_work/versions/fair-final-command-coordinates-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增逐预约原生对象映射限定验收：62份归档独立重算，三组各426次决策、20个
观察边界及tick5897终局一致；188预约全部关联构造和真实执行对象，68个双方
同时执行tick、20个对象跨tick复用，所属清理通过。最终33项专项通过，含真实
归档重验。未证明原生命令内部动作字段与坐标转换，一般拒绝、完整mask与全部
随机覆盖仍开放。Full128快照1792913752已提交182组/364根/46592模拟、继续
搜索且未终局；terminal第17步完成、第18步验证中。G0/G1及34/43不变。
详见[_spec_work/versions/fair-final-reservation-map-v1/EVIDENCE.md](_spec_work/versions/fair-final-reservation-map-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增虚分派实际目标限定验收：独立v4插件显式记录X8，三组完整对局各426决策/
20个观察边界及tick5897终局一致；188次调用的目标、对象、参数、LR与返回值
全部配对，62份归档及所属清理通过。最终34项专项通过，含真实归档重算。
只证明本轨迹调用点，逐预约映射、一般拒绝、完整mask与全随机覆盖仍开放。
Full128快照1792911277已提交180组/360根/46080模拟，tick5906继续、未终局；
terminal第17步进行中。G0/G1及34/43不变，未启动正式迁移或放量。详见
[_spec_work/versions/fair-final-dispatch-command-v1/EVIDENCE.md](_spec_work/versions/fair-final-dispatch-command-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增原生动作执行链限定验收：三组完整对局各426次决策、20个周期观察边界
一致，观察到188次ct86执行器与位置helper完整配对，返回码均0；所属清理通过。
两次启动失败保留，过滤版正常终局，最终29项专项通过。未证明一般合法拒绝、
虚分派目标寄存器或完整mask；G0/G1与34/43保持开放。Full128已提交179组/
358根/45824模拟，tick5874继续、未终局；terminal恢复已到第17步，尚待收尾。
详见[_spec_work/versions/fair-final-live-command-v1/EVIDENCE.md](_spec_work/versions/fair-final-live-command-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增位置拒绝条件的独立CPU核验：原始81条AArch64指令在14,676个合成结构体
样例上与参考公式一致，全部指令及六种返回码覆盖；18项专项通过。另证除500
的全signed32算术公式。合成CPU证据不替代游戏可达性、terrain函数体或完整mask
证明，G0/G1与34/43不变。Full128快照1792907076提交177组/354根/45312模拟，
tick5864继续、未终局；terminal恢复仍运行。详见
[_spec_work/versions/fair-final-position-guards-v1/EVIDENCE.md](_spec_work/versions/fair-final-position-guards-v1/EVIDENCE.md)。
以下均为此前阶段及时间点记录。

本次补齐最早合法延迟的原生有限诊断：四根37组/74次快照续演一致，覆盖
delay2/4/15/18/25及下一档；合法拒绝与单方就绪均0组，独立内容验收及清理通过。
这只补充旧delay1/30探针，未证明一般pending不可达，G0/G1与34/43不变。
Full128快照1792905521已提交176组/352根/45056模拟，tick5804继续、未终局；
terminal恢复仍待验。详见[_spec_work/versions/fair-final-affordability-boundaries-v1/EVIDENCE.md](_spec_work/versions/fair-final-affordability-boundaries-v1/EVIDENCE.md)。
公开回归794通过/27项缺私有附件跳过，独立阈值23通过、恢复与交接82通过、
主工程211通过，集合不累加。以下为此前阶段及时间点记录。

本次完成全局快照搜索的原生限定验收：4根/512有效模拟、2对隐藏干预结果一致，
1483次快照创建与释放、551次恢复及所属双引擎清理通过。完整搜索局仍待验。
新增terminal恢复结果到补充采集包的独立交接，联合82项工程测试通过，含39项
交接测试；真实terminal未完成，真实交接与round2尚未开始。Full128最新快照
提交175组/350根/44800模拟，tick5744继续。G0/G1及34/43保持开放。
详见[_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md](_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md)
及[_spec_work/versions/fair-final-retry-collection-bridge-v1/EVIDENCE.md](_spec_work/versions/fair-final-retry-collection-bridge-v1/EVIDENCE.md)。
发布复核：公开794通过/27项缺私有附件跳过，独立恢复与交接82通过、主工程211
通过；2244项源码清单通过，凭据扫描零发现。下面保留此前阶段与时间点记录。

新增独立terminal超时恢复：旧8小时选优失败保留，第15步完整状态核验通过，
43项联合工程测试通过；24小时续跑已在.35 GPU1启动，真实terminal仍待验。
Full128同局恢复174组/348根/44544模拟至tick5728并继续；独立全局MT搜索
完成1/4根。G0/G1与34/43不变，不启动正式迁移或放量。详见
[_spec_work/versions/fair-final-selection-timeout-v1/EVIDENCE.md](_spec_work/versions/fair-final-selection-timeout-v1/EVIDENCE.md)。
以下为此前测试和运行快照，不作当前存活或准入证明。
本轮公开复核：794通过/27项缺私有附件跳过，独立恢复回归43通过，主工程211通过；
2230项源码清单通过，凭据扫描零发现；测试集合不累加。

新增rng-callsite-audit-v1，本机实际重算18处直接MT初始化参数：11栈相对、
6分配器返回、1传入地址；未知来源保留。25项专项、联合130项通过，不据此
认定对象持久性、固定速猪可达性或快照覆盖。五机SSH仍超时，冻结部署保持。
G0/G1及34/43不变，详见该版本EVIDENCE.md。以下为此前测试和运行记录。
本轮发布复核：公开794通过/27项缺私有附件跳过，2220项清单通过，凭据扫描零发现。

新增global-search-v1独立入口，逐根核对完整native+全局MT摘要、恢复身份与句柄
释放；38项专项、联合139项通过（7.81秒）。真实父证据重验，353项载荷已准备，
新原生Full128实测待执行。五机SSH均超时，原Full128与terminal当前状态待核验，
未因失联重启或修改冻结部署。G0/G1及34/43保持开放，详见
[_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md](_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md)。
发布复核：公开770通过/26项缺私有附件跳过，主工程211通过，2212项清单通过，凭据扫描零发现。
以下运行快照与测试数量均为历史记录。

新增独立QEMU插件测量环境，官方10.0.13构建与33份归档内容核验通过。
修复vCPU槽复用、块内PC误判和跨进程地址绑定；真实合成指令检查通过，
监督封存显式区分正常退出与完整终局后受控关闭。五次原生失败保留，第六次
三组完整局通过限定验收：426条决策、20个观察边界完全一致，44条实际入口
记录及reset正向对照齐全。只证明本轨迹八个已知入口，原始审计私有保留。详见
[_spec_work/versions/fair-final-plugin-runtime-v1/EVIDENCE.md](_spec_work/versions/fair-final-plugin-runtime-v1/EVIDENCE.md)。
联合139项、公开734项通过/24跳过、主工程211项通过，2202项源码清单通过；集合不相加。
Full128快照1792751687：133组/266根/34048模拟提交，tick4834玩家0搜索中；
terminal仍运行，冻结部署与G0/G1、34/43不变。以下为历史进度。

最新工程为已知随机入口的独立执行追踪及内容验收，61项专项通过。四次原生
尝试均失败并保留：规则绑定缺失已修；扩大独立启动等待后仍遇SIGALRM终止，
尚无有效执行日志，不把空日志当作未调用证据。G0/G1与34/43不变，详见
[_spec_work/versions/fair-final-random-execution-v1/EVIDENCE.md](_spec_work/versions/fair-final-random-execution-v1/EVIDENCE.md)。
Full128快照1792744059：127组/254根/32512模拟提交，tick4604玩家0搜索中，
worker及双引擎在线、未终局；terminal选优仍运行。冻结部署不变，下段为历史快照。
本轮2162项源码清单通过，公开回归663通过/17项缺私有附件跳过（47.74秒），
主工程211通过（23.99秒）、联合163通过（8.43秒），凭据扫描零发现；集合不相加。

最新工程补齐：terminal选优结果到补充采集包的独立交接入口，强制父内容验收、
继承冻结训练字节并复核checkpoint身份；35项专项、联合78项通过（58.28秒）。
成功交接为mock工程测试，真实terminal仍运行，真实交接与round2原生待验。
Full128快照1792741500：125组/250根/32000模拟提交，tick4516玩家1搜索中，
未终局；冻结部署、G0/G1与34/43保持原状态。下列快照为历史记录。
本轮2143项源码清单通过，公开回归603通过/16项缺私有库跳过（48.29秒），
主工程211通过（22.40秒），凭据扫描零发现；各集合不相加。

原生开局先验诊断已完成：1024个预注册seed与第二新进程64个seed复测通过
独立验收，17份归档及所属引擎清理通过，跨进程可观察牌序一致。21项事前检验
未发现显著偏离（最小p=0.0772149，阈值0.01/21）；只补有限经验诊断，不证明
精确均匀、全排列支持、隐藏RNG一致或历史条件后验。私有原始审计不公开。
Full128快照1792738671：123组/246根/31488模拟已提交，tick4470玩家1搜索中，
无失败、未终局；terminal选优仍运行，冻结部署与G0/G1、34/43保持原状态。
本轮2140项源码清单通过，公开回归584通过/16项缺私有库跳过（49.01秒），
主工程211通过（24.21秒），
凭据扫描零发现；各集合不相加。以下段落保留此前快照。

新增条件分析：真实history-v2运行时对1,981种时间与最早到期结果组合核对通过。
961种无拒绝组合保持双方同步；928种较早ACT私有拒绝组合产生单方就绪。
结果环境为合成值，只证明条件时序关系；合法原生拒绝可达性仍待证。
11项专项、联合45项通过，G0/G1与34/43不变，Full128冻结部署保持原样。

最新补证：双独立原生进程完成有战斗的预约快照续演。tick110/600/1200六个根
均含双方pending预约，每根两条各60tick战斗分支逐tick一致，完整根恢复通过。
每进程双方实际ACT14/13次、正常取消5次，实体/公开血损证据齐全；21份归档
独立验收，句柄和所属引擎清理通过。55项专项、联合240项工程测试通过（20.67秒）。
全局MT只读观察未变化，不证明固定速猪消费者不可达或全部随机状态覆盖。
Full128快照1792736248：121组/242根/30976模拟，tick4392搜索中、未终局；
开局采样801/1024，terminal选优待验。冻结部署及34/43不变，G0/G1保持开放。
发布复核：2131项源码清单通过，公开回归573通过/16项缺私有库跳过（72.25秒），
主工程211通过（36.70秒），凭据扫描零发现；各集合不相加。

此前独立修复：单比赛执行通道的全局MT快照与摘要覆盖通过限定验收。
两个新原生进程、8个边界全部恢复2504字节，重复restore及两条各10步WAIT
续演一致；新摘要感知全部受控扰动，19份归档独立校验。新47项与随机诊断
联合185项通过（11.37秒）。固定速猪实际消费者和有战斗续演仍待证，
不将单对象覆盖当全部随机门通过。旧引擎与后台冻结部署保持原样。
Full128快照1792734434提交119组/238根/30464模拟至tick4322，tick4342继续；
开局545/1024、terminal选优运行。G0/G1与34/43不变，G2–G8未启动。
发布复核：2118项源清单通过，公开回归518通过/16项缺私有库跳过（62.13秒），
主工程211通过（36.07秒），凭据扫描零发现；各测试集合不相加。

此前原生诊断：全局MT的加载注册入口及三段消费者代码已绑定。
两个独立引擎、共8个边界均证实该对象的受控扰动未被快照恢复，state-digest
也未检测其变化；修复原字节和所属引擎清理通过。WAIT轨迹中它保持不变，
尚未证明固定速猪战斗实际使用或受其影响，不宣称已发现实战非确定性。
新增33项，与alias/shuffle/seed联合138项通过（6.06秒），原始内存审计私有保留。
Full128快照1792733078提交118组/236根/30208有效模拟至tick4302，tick4322继续；
开局369/1024，terminal选优仍运行。G0/G1及34/43开放，冻结部署保持原样。
本轮发布复核：2106项源清单通过，公开回归471通过/16项缺私有库跳过
（39.82秒），主工程211通过（23.46秒），凭据扫描零发现；各集合不相加。

此前补查：随机初始化直接引用之外，新增无条件跳转入口闭包审计。
18处直接引用扩展为19条边，定位额外time(NULL) seed路径及全局对象；
实际入口可达性、固定速猪用途和快照覆盖仍待证，未发现已证实的实战影响。
新增31项，与洗牌/种子联合105项通过（5.37秒），冻结部署保持。
Full128快照1792731888已提交117组/234根/29952有效模拟，tick4302继续搜索、未终局；
开局采样177/1024，terminal选优运行。G0/G1及34/43保持开放，未五机放量。
本轮发布检查：2094项源清单通过，公开回归439通过/15项缺私有库跳过
（38.08秒），主工程211通过（22.14秒），凭据扫描零发现；测试集合不相加。

上一阶段：基于fetch确认的主线`18e984c`，active独立恢复重试通过双归档内容验收。
失败父保持失败，子恢复第5→7步；网络、AdamW、RNG、采样游标及选优状态一致。
此验收未重新核验原始对局来源，不代表G2或训练准入。独立terminal选优已启动，
结果待验；本地派发公开路径边界修复32项测试通过，未改运行中的冻结部署。
独立1024seed开局采样与新进程64seed复测已启动，34项工程测试通过；
原生采样未完成，21个事前检验尚无结论，不代替历史条件后验或全部随机机制覆盖。
Full128已提交116组/232根/29696有效模拟至tick4266，tick4294继续搜索，未终局。
G0/G1与34/43登记保持开放，G2–G8及五机放量未启动。
本轮复核：2088项公开源码清单通过，公开回归414通过/9项缺私有库跳过
（37.95秒），主工程211通过（21.97秒），凭据扫描零发现；各集合不累加。

此前补证：基于fetch确认的主线`11bc2ac`，完成手牌临时随机路径静态审计。
14段字节摘要、18处MT初始化直接引用函数归属及正常返回清理控制流通过；
手牌临时生成器的seed来源与旧tick-seeded debug路径分别定位，不把静态引用
当作实际可达性。新24项与旧seed50项联合74项通过、零跳过，3.79秒。
其余16处引用、间接分派、原生路径观测和先验校准仍待证，G0/G1与34/43保持开放。
后台快照：114组/228根/29184有效模拟至tick4170，tick4230搜索中、未终局；
active重试仍运行，terminal选优未启动。冻结部署保持原样。
发布复核：2075项公开源码清单通过；公开回归348通过、9项缺私有库跳过
（38.62秒），主工程211通过（23.99秒），凭据扫描零发现。各集合不累加。

此前基于主线`f0707ee`，完成原生种子生命周期限定验证。
两个独立引擎、各四阶段的快照续演与同seed状态一致，同进程重复reset一致，
所属引擎清理通过。50项本机测试通过；原失败证据保留，未改变冻结运行部署。
首次reset前时间缓存已填充，本次seed增量均0；未覆盖全部随机组件或原生增量1。
Full128仍在.35后台运行：112组/224根/28672有效模拟至tick4106，未终局；
active恢复重试仍运行，terminal选优未启动。G0/G1与34/43保持开放。
本轮2067项公开源清单通过；公开回归331通过、2项缺私有库跳过（57.77秒），
主工程211通过（36.01秒），凭据扫描零发现。各集合不累加。

上一阶段核验：fetch全部远端分支确认更新前最新主线为`416c616`，已有审计分支已整合。
当时2056项公开源清单通过；公开工程回归282项通过、1项因私有游戏库缺失跳过，
主工程211项通过。新静态参数审计的本机15项通过，其中真实固定库检查仅在本机运行；
不把工程测试或缺少附件的公开副本当作原生完整局验收。

2026-10-23 Full128优先推进：旧段在tick3922保存，103组、206根、26,368模拟的
递归父链与244项冻结源码已核验。用户指定新段24小时，启动、worker、探针及
验收同步支持86400秒，保留30分钟保存余量；51项专项通过9.16秒。
同局新段已在.35启动，新进程已重演211条记录至tick3922并开始新根，
完整终局及最终独立验收仍待证据。active父最后
trainable恢复阶段超总时限exit124，原证据保留，整体门与34/43保持开放。
后台运行期间，一般provider原生local四根512有效模拟已独立验收通过：
tick110/142两组隐藏状态配对一致，增量锚点、模型身份与所属双引擎清理通过。
它不覆盖原生pending/fallback或总体先验校准。队列mode合法WAIT轨迹已观察到
bit0-set；进一步静态核对ct86保存参数只显式读取bit1，不证明整个执行器等价或
合法拒绝不可达。快照1792718966：Full128提交105组至tick3982，tick4036搜索中，
双引擎在线且无失败报告。中心只读空间206.93GiB，两代正式数据容量仍未验。

后台继续期间，独立解冻恢复重试已在.35 GPU1启动，原超时父仍为失败。
新增双归档验收与显式重试链选优交接，保留冻结实现、完整恢复状态与原门槛；
缺链、身份复用及篡改拒收。真实重试仍运行，terminal选优尚未启动。
恢复与选优交接联合工程测试141项通过，182.68秒，不替代原生恢复验收。
快照1792720941：Full128已提交107组/214根/27392模拟至tick4038、未终局。
随机状态全覆盖仍有缺口；G0/G1开放、34/43保持，不开展五机放量。

## 目录与入口

- [`firstlight_CR/`](firstlight_CR/)：主工程，包含原生适配、策略模型、旧 PPO 基线、FAIR 搜索及测试。
- [`agent-handoff/`](agent-handoff/)：ChatGPT ↔ Codex 专用文字交接：迁移步骤、代码审查、验收证据与回复模板；不属于运行代码。
- [`_spec_work/versions/`](_spec_work/versions/)：隔离的增量实现、回归与证据，包括历史信念、一般退化路径、迁移消费、恢复、CUDA 推理和原生队列审计。
- [`冻结规范`](_spec_work/evidence/fair-final-v1-20261019/spec/clash_royale_fair_final_v1/README.md)：训练语义、数据契约、验收门及实验矩阵。
- [`整体进度`](_spec_work/evidence/fair-final-v1-20261019/DOCUMENT_EXECUTION_PROGRESS.md)与[`闭合清单`](_spec_work/evidence/fair-final-v1-20261019/G0_G1_CLOSURE_CHECKLIST.md)：完整历史记录。
- [`成果索引`](docs/RESULTS.md)、[`复现说明`](docs/REPRODUCING.md)、[`发布边界`](docs/PUBLICATION.md)。

部分历史文档使用本机绝对路径和旧机器部署目录，保留为历史上下文；本仓库入口以相对路径为准。`firstlight_CR` 的历史介绍含旧模型发行说明，本公开快照的实际附件范围以 `publication/source-manifest.json` 为准。

## 快速验证

使用 Python 3.12 或 3.13。先创建独立虚拟环境，安装测试依赖；CPU 版 PyTorch 足以运行下面的工程测试。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python tools/check_public_snapshot.py
.venv/bin/python tools/run_public_checks.py
python tools/run_main_engine_checks.py    # 另测当前 firstlight_CR；不替代冻结历史版本
```

原生对局、完整训练及静态二进制审计需要自行准备匹配的游戏引擎、资源及模型。仓库附有适配源码、构建输入源码和身份摘要，第三方引擎二进制、提取资源、私有对局及模型权重单独保留。详见复现说明。

## 目标完成后的更新

每个目标完成后同步相应实现、测试、文档和证据摘要，运行公开检查后提交并推送本仓库。更新流程与凭据处理要求见 [`AGENTS.md`](AGENTS.md) 和 [`维护流程`](docs/MAINTENANCE.md)。长时间运行的任务继续使用其冻结部署，仓库同步独立进行。

沿用原工程的 [Apache-2.0](LICENSE) 与 [NOTICE](NOTICE)；第三方材料另受各自条款约束。
