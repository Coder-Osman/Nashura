# 阶段成果与证据索引

## 2026-10-23 随机初始化跳转入口补查

独立rng-alias-audit-v1核对18处直接引用及19条跳转闭包边，定位time(NULL)作为
seed初始化全局对象0x19fc368的额外路径。绑定三段完整字节、PLT及导入符号；
不将EH范围当真实入口，不宣称路径已执行、固定速猪使用或快照覆盖。
这是一项明确待验项，非实际搜索受墙钟影响的证明。冻结部署保持原样。

本机新增31项，与洗牌/种子联合105项通过、5.37秒；对应真实库重算及五类篡改
检查在缺私有库的公开副本中跳过。Full128快照1792731888：117组/234根/29952
有效模拟，tick4302玩家1继续搜索、未终局。开局采样177/1024，terminal选优
运行，原生round2待验；G0/G1与34/43保持开放，不启动G2或五机放量。
发布复核：2094项源清单通过，公开回归439通过/15项缺私有库跳过、38.08秒；
主工程211通过、22.14秒；凭据扫描零发现。各测试集合重叠，不累加。

## 2026-10-23 恢复重试验收与独立开局诊断

active-migration-v1/retry-completion-evidence-20261023.json绑定失败父、成功retry、
外部launch/回执/验收SHA及真实选优交接。重试第5→7步完成，exit0、9430.322秒；
双归档内容验收通过，网络/AdamW/RNG/游标/选优状态一致，冻结部署复核通过。
native_game_provenance_reverified=false，原始对局来源未由此次验收重验；
不作G2、完整迁移或训练准入。父仍保留超时失败。真实terminal选优已在.35 GPU1
启动，独立28800秒，仍运行中；原生round2待验。

修复SSH派发控制器的公开目录边界误判，源CR和公开导出布局/.git文件覆盖共
32项通过0.158秒。未修改运行中冻结包。独立prior-sampling-v1预注册1024seed、
第二新进程64seed复测与21个Bonferroni校正检验；34项工程/篡改检查通过1.325秒，
原生采集进行中，无分布结论。首attempt远端缺SciPy在引擎启动前失败并保留；
统计改为本地惰性导入，新attempt独立，不修改seed及检验。

Full128快照1792731064已提交116组/232根/29696有效模拟至tick4266；
tick4294玩家1搜索中，无失败、未终局。G0/G1、34/43保持开放，不五机放量。
中心205.514GiB为瞬时可用量，两代数据容量未验。原始私有归档和权重独立保留。
发布复核：2088项公开源码清单通过；公开回归414通过、9项因缺私有库跳过，
37.95秒；主工程211通过、21.97秒；凭据扫描零发现。测试集合重叠，不相加。
公开测试依赖补入SciPy，统计仍只在本机运行，采集进程不新增依赖。

## 2026-10-23 手牌临时随机路径静态审计

`fair-final-native-shuffle-audit-v1/EVIDENCE.md`及`static-report.json`记录固定库的
14段字节摘要、18处MT初始化直接引用/唯一EH函数归属、手牌seed来源及同一栈对象
的generate/clear。正常返回控制流225指令/状态组合均经clear；分析排除异常和
非局部退出，不作全局调用图证明。ct1548/subtype55通过两级表分派及vtable另行定位。

本机新专项24项与旧seed50项联合74项通过，3.79秒、零跳过；缺私有库的公开版本
跳过对应静态重算/篡改检查，不把跳过当作原生通过。16处其它引用、间接分派、
实际路径观测、排列分布与先验校准仍待验。G0/G1及34/43登记保持开放。
Full128后台提交114组/228根/29184有效模拟至tick4170，未终局；active重试仍运行。
G2–G8、正式训练及五机放量未启动，运行中的冻结实现未变。
发布复核：2075项公开源清单通过；公开回归348通过/9项缺私有库跳过，38.62秒；
主工程211通过，23.99秒；凭据扫描零发现。各集合不累加。

## 2026-10-23 原生种子生命周期限定验证

`fair-final-native-seed-audit-v1/EVIDENCE.md`及`evidence-summary.json`绑定独立双进程
原始归档与验收SHA。合法WAIT轨迹各四阶段的只读检查、过期身份拒收、快照重复
续演、同进程重复reset及跨进程同seed状态一致；所属引擎清理完成，17份归档核验。
静态核对四段函数、构造器注册及time导入；本机50项工程测试通过1.68秒。

首个探针复用atomic连接执行90 tick warmup失败，原证据保留且验收拒收；新attempt
只在探针更换连接，未改冻结runtime。首次reset实际距base约35秒，缓存此前已非零，
两进程seed增量均0；未验证15秒内首次reset、原生增量1及全部隐藏随机机制。
完整搜索与恢复终局、一般原生边界仍待验，不新增整体门或34/43登记PASS。

Full128继续至112组/224根/28672有效模拟、tick4106；active恢复retry仍运行，
terminal选优未启动。G2–G8、正式训练及五机放量未启动。
公开回归331项通过、2项缺私有库跳过，57.77秒；主工程211项通过，36.01秒。
2067项公开源清单通过，凭据扫描零发现；测试范围重叠，不累加。

## 2026-10-23 超时恢复重试与显式选优交接

`fair-final-active-migration-v1` 新增独立恢复启动器、运行时与双归档验收器。
只接受四阶段 `[0,0,0,124]` 的超时父，287份输入683466574字节原样继承，
只恢复解冻第5→7步，父继续保留失败。原生retry已在.35 GPU1运行，独立28800秒；
尚待完成与独立内容验收，未启动terminal选优。

选优交接显式绑定失败父和成功retry，重核父失败记录/父子launch及回执/验收器SHA，
保留完整checkpoint和冻结部署；新状态与原四阶段成功状态分别处理。
工程测试含真实子进程及篡改拒收，但小模型夹具继续被真实日程门拒收。
测试与证据身份见 `active-retry-evidence-20261023.json` 和对应XML；失败报告保留。
联合工程测试141项通过，182.68秒；公开回归282通过/1跳过，57.91秒；
主工程211项通过，36.11秒。2056项公开源清单通过，凭据扫描零发现。
测试范围重叠，不累加；不代表原生完整恢复、terminal选优或整体门已通过。

Full128继续：107组/214根/27392有效模拟至tick4038，无失败、未终局。
随机状态覆盖未闭合；整体G0/G1及34/43保持，G2–G8、正式训练和五机放量未启动。

## 2026-10-23 组合provider原生local验收及执行参数审计

`fair-final-general-fallback-v1/native-local-evidence-summary.json`：四根512有效模拟、
tick110/142两组真实隐藏状态配对、未完成边零回传与引擎清理独立通过。
核验343项源码及33份归档。首锚点10.953秒，随后0.135/1.069/0.221秒；
只有四根，不外推恒定成本。原生pending/fallback、总体先验校准和完整终局仍待验。

`fair-final-native-execution-flags-v1/static-report.json`：固定库静态控制流核对
保存的参数2/3仅有两个显式bit1测试。15项本机分析器/篡改测试通过；对opaque
helper、整个执行器和两条队列不作等价证明，不解释未证实的错误码或宣称拒绝不可达。

本轮公开回归282项通过（35.800秒），缺私有库的单项跳过；主工程211项通过
（20.43秒）；2045项公开源清单通过，凭据扫描零发现。测试集合分别记录、不累加。
后台Full128继续：快照1792718966已提交105组，tick4036搜索中、无失败报告。
整体G0/G1与34/43登记保持，G2–G8未启动。以下为此前记录。

## 2026-10-23 原生队列mode限定验收

fair-final-native-mode-audit-v1独立绑定固定libg的27条predicate指令，
原生合法WAIT轨迹在tick90/1200/3600/5400均读得world_type=3、
predicate_bit0=1，对应bit0-set分支；不是旧queue审计中的bit0-clear/age20路径。
过期身份拒收、只读digest不变、快照重复续演和所属引擎清理独立通过。
32项工程/篡改测试通过；不证明所有实战分支、mask等价或合法pending可达。
下一项应针对bit0-set的ct86调用条件取证，不用有限轨迹声称全局不可达。
中心/home持久盘最新只读可用206.931198GiB，越过200GiB瞬时底线，
两代正式容量与正式中心链路仍待验；未清历史数据或调整原门槛。

## 2026-10-23 一般provider原生local路径验收启动

Full128后台继续；另用独立29771/29772双引擎部署general-fallback组合provider，
复用冻结history-v2真实轨迹，注册tick110与后继根、原始与隐藏牌序扰动配对，
每根128有效模拟。新增独立验收器校验外层组合及底层独立构造身份、抽牌链、
访问数、边界回传、恢复和清理；工程fixture不作为原生通过证据。
attempt为native-general-local-1792716408200694000，首根128模拟完成561.9998秒，
隐藏配对根计算中，四根结果待验。54项工程/验收测试通过。
此探针只验证local分支，原生pending/fallback、总体先验及G0/G1整体仍开放。

## 2026-10-23 Full128同局24小时续跑

旧段在tick3922完整边界保存并清理。用其SHA固定归档恢复244项源码，显式
加载冻结搜索协议，103组/206根/26368有效模拟和递归父链核验通过。新段
native-search-segment-1792715657103653000在.35启动，1 actor、2专属引擎。
用户指定86400秒上限，1800秒完整边界保存余量；历史21600秒记录继续可验。
启动器、worker、探针、收取及终局验收保持相同时限与源码身份语义。
51项专项通过9.16秒，见fair-final-search-continuation-v1/tests-24hour-20261023.xml；
工程测试不代替完整原生终局。新进程已重演211条日志至tick3922并开始新根，
恢复报告passed_replay_only；完整终局及最终独立验收仍待完成。
active父benchmark/reference/frozen成功，trainable总时限耗尽exit124，跨进程
恢复整体验收尚未通过；整体门与正式中心200GiB门保持，未启动正式训练。

## 2026-10-23 远程选优派发/回收原型

最新主线为`5c54e0478747d30be2472f855c06a871d8866406`。新增
`selection_remote_runtime.py` 与 `selection_ssh.py`：绑定目标GPU UUID及资源预检、
GPU独占lease、拒绝未知SSH主机密钥、原子SFTP上传，要求远端上传树与配置清单完全
一致；回收时交叉校验远端配置/进程/失败元数据的SHA、字节数和JSON内容，下载产物
后仍须通过既有独立选优验收器。

新专项29个测试案例通过；与handoff、execution和active verifier联合93项通过98.774秒，
重叠测试不累加，详见`fair-final-active-migration-v1/remote-selection-evidence-20261023.json`
及`tests-selection-remote-joint.xml`。SSH及远端文件系统均为模拟：没有真实主机连接、
GPU启动、父恢复或terminal选优；因此不新增G0/G1门PASS，不代表正式G2/G3或模型强度。
运行准入字段保持false，冻结部署保持原样。

核对日期：2026-10-23。本文是公开快照说明；完整历史见工程进度与冻结验收清单。

## 2026-10-22 既有成果与此前基线

最新基线为`8f68763803207890351f574ab7191a5a613cb505`，fetch全部远端分支、
本地HEAD/origin/main逐项核对一致，更新前2010项公开源SHA通过。新增独立预算
选优执行工具和终止内容验收：只运行交接中的冻结代码，保留完整第7步状态，
超时/中断清理独立进程组，失败隔离；校验terminal实际checkpoint、父历史、
latest/best游标及RNG和完整best继承。修复第12轮额外洗牌和Tensor字典比较。
联合64项通过184.31秒，包含新26项、此前17项及21项，不累加。真实子进程
执行使用显式fixture，小模型验证EARLY_STOP和MAX_EPOCHS，不作为原生PASS。
真实交接、原生terminal选优及SSH派发/回收仍待完成；证据见
`_spec_work/versions/fair-final-active-migration-v1/selection-execution-evidence-20261022.json`。

本轮发布复验：冻结参考检查220项通过（80.95秒），主工程CPU检查211项
通过（49.75秒），2015项公开源清单校验通过，gitleaks未发现凭据。
上述测试各自记录，不与64项专项测试累加，也不替代原生验收。

以下为此前成果记录。

本轮在最新主线`f9e0958e8fe671eaa22e42319dcfc599e89c2a89`上新增独立选优
交接准备工具。强制新版原生352/256/96日程和六checkpoint恢复验收，原字节
保留冻结包及第7步完整模型/AdamW/RNG/游标/选优历史，拒收失败父、fixture门和
旧batch链路；只准备、不执行。专项17项通过50.62秒，联合38项通过109.81秒，
包含前者，不累加。真实原生交接尚未运行。证据见
`_spec_work/versions/fair-final-active-migration-v1/selection-handoff-evidence-20261022.json`。
本轮冻结参考220项通过78.06秒、主工程211项通过51.41秒，分别为CPU工程检查。

上一轮fetch远端确认主线`da8ca6c12653add5cc4a8667f4809db9625a0ac7`，
清单2000项源SHA与工作区一致。新增active独立归档验收器，外部固定launch/
回执，核验部署、数据、四阶段独立进程和六个checkpoint的真实恢复状态，默认
强制352行、batch256、microbatch1、尾批96。专项21项通过60.40秒，工具/恢复/
验收联合31项通过98.27秒，范围重叠不累加；fixture不作为原生通过。
证据见fair-final-active-migration-v1/verifier-evidence-20261022.json。原生reference
已完成256行和96行两次更新，两个恢复阶段及最终独立验收仍待完成，整体门状态不变。

新增最新主线独立原生验证工具，见`fair-final-active-migration-v1/EVIDENCE.md`。
277文件载荷绑定六个active源码、独立scalar参考与新实现恢复身份；10项CPU
测试最近重跑通过32.96秒。首次CUDA解冻阶段AdamW更新超容差，失败归档287文件
保留：近零梯度差异被优化器放大。已修复教师/学生解冻时均走逐位置路径，冻结仍
合批；learning 11项、migration 26项通过。修复后GPU1六对CUDA损失/梯度/AdamW
比较通过，解冻三对最大梯度差0，快照1792676859已进入reference。冻结/解冻
跨进程恢复及最终独立归档核验待完成，不新增整体门PASS。主工程211项通过36.39秒，
冻结参考220项通过50.25秒，分开记录不累加。单帧配对加速比中位数1.01174，
冻结两个可ACT帧约6.14/6.54倍、解冻约1倍；不代表完整训练吞吐。

| 成果 | 实现与证据 | 证据边界 |
|---|---|---|
| FAIR 信息帧、预约时钟、自回归合法动作、完成边界 MCTS、回放和恢复 | firstlight_CR/native_runner/training/fair_final 及 tests/test_fair_final_* | 局部、内存及原生专项分别记录，整体门开放 |
| 增量信念与局部重条件化 | _spec_work/versions/fair-final-history-v2 | 冻结源码身份可核对；原生附件外置 |
| 局部路径不支持时的一般历史退化 | fair-final-general-fallback-v1/EVIDENCE.md | 73 项相关测试；内存双方 pending 四根 512 次模拟；不证明原生合法可达 |
| 原生合法预约专项 | fair-final-legal-reservations-v1 | 已有 57 分支、114 次重复续演，未观测拒绝；有限覆盖 |
| 迁移数据、消费、选优与断点恢复工具 | fair-final-migration-{series,selection,integration,cache}-v1 | 三局 1,044 行、FP32 batch256跨进程三次更新恢复独立通过；terminal选优、原生round2与正式迁移仍待验 |
| 迁移 KL 合批与教师条件化缓存 | fair-final-migration-batch-v1/EVIDENCE.md | 六对真实 CUDA 损失/梯度/AdamW对照通过；解冻encoder保留原反向图，完整五阶段链路仍待验 |
| 完整搜索局分段续跑与前缀验收 | fair-final-search-continuation-v1/CONTRACT.md | 同一局前缀保存与恢复，不是完整终局 PASS |
| 原生命令与队列静态审计 | fair-final-native-{command,queue}-audit-v1 | 联合 42 项通过；静态 CFG 不证明运行时分支或合法掩码等价 |
| 延迟补牌离线诊断修正 | fair-final-delayed-prior-audit-v1 | 三局 1,044 帧零排除、原生轨迹牌区对照；不证明总体先验校准 |
| 嵌套历史评测种子审计 | fair-final-migration-integration-v1/NESTED_SEED_EVIDENCE.md | 四组3,200条记录新增1,600个排除seed；联合190项通过，5条结局不明确记录不作WDL标签，原始附件外置 |
| 合批父包到采集包的确定性身份交接 | fair-final-migration-integration-v1/collection-runtime-lineage-evidence-20261022.json | 24项通过；实际276源码SHA复核、273父文件保持；仅本地准备，原生round2仍待验 |
| 后续采集预检与有界引擎worker | fair-final-migration-integration-v1/bounded-collection-verification-20261022.json | 43项联合测试通过；278源码采集包、273父文件保持；夹具不替代原生采集与独立整局验收 |
| 后续采集归档独立验收与多轮父身份 | fair-final-migration-integration-v1/selected-collection-verifier-evidence-20261022.json | 47项联合测试通过；重验终止选优父、整局与原始终局、种子来源和所属清理；仅归档内容及fixture范围 |

以上版本目录均位于 `_spec_work/versions/`。测试集合存在重叠，表格数字不相加。

## 2026-10-22 主工程修复分支（已整合、CPU验证通过）

- 已通过fast-forward整合分支 `audit/ci-kl-puct-inheritance-20261022` 最新提交 `8195a5a07b8bf7d9533fc7a3bfbe83b0c5083a99`，保留本机未发布成果，8个新增或修改源码同步回工作区。为当前 `firstlight_CR` 新增独立单测入口/CI；原冻结历史版本公开检查保留。
- 原生迁移批量KL在主工程加入独立批量Delay条件计算与校验。**源独立版本 `fair-final-migration-batch-v1` 的真实CUDA数值与原生恢复证据仍是权威参考**，不可用本分支单测取代；编码器解冻时保留逐位置学生反向图与逐位置KL加法顺序。
- PUCT新增先验首访、旧字典序首访基线；根噪声显式区分 `training/evaluation/ablation_no_noise`，协议SHA随配置变化。
- 权重继承新增基于逐Tensor一致性的覆盖率报告，当前能被证明直接继承的是语义Encoder，新Policy/WDL Head继续蒸馏，不凭相同形状虚报。
- 本机当前主工程211项通过24.12秒，冻结参考版220项通过39.00秒，分别属于CPU工程测试。所拉取提交的两套GitHub CI亦通过：[当前工程](https://github.com/Coder-Osman/Nashura/actions/runs/39779254288)、[冻结参考](https://github.com/Coder-Osman/Nashura/actions/runs/39779254266)。新实现CUDA与batch256原生恢复仍待验，不增加G0/G1 PASS，不启动G2或正式训练。交接与回执见 `agent-handoff/`。
- 归档验收器、五阶段归档核验及旧batch-v1在最新active源码下的兼容性检查75项通过69.40秒，3项因缺少私有冻结部署附件跳过。此项与上面的测试集合存在重叠，不累加；详细身份见 `agent-handoff/INTEGRATION_2026-10-22.json`。

## 当前运行任务的最后已知快照

快照时间戳1792681423/1792681789，以及此前独立恢复验收：

- Full128 搜索局旧段已提交86组后归档；新段恢复后已提交96组、192根、24,576次有效模拟至tick3644，tick3672玩家0搜索中，尚未原生终局。该任务继续使用原冻结部署。
- 确定性父训练探针已结束，执行1次FP32更新；尚无该更新后的验证选优结论。
- 严格确定性恢复reference与resumed均正常结束，完成归档及独立内容/执行链验收；模型、AdamW、RNG、采样游标/下一批及选优历史严格一致。证据见migration-cache-v1/native-deterministic-resume-verified-20261022.json；仅三局工程范围，不证明终止选优或正式G2。
- 旧迁移KL链路benchmark/training/reference/resumed均exit0；selection耗尽14400.468秒总预算后退出124，无terminal报告，最后持久化第7步/3个epoch。归档仍在下载，不作为合格选优父；六对单帧加速比中位数1.376，不代表整轮吞吐。
- 最新active链路的benchmark六对通过，reference已完成4次冻结更新和2个epoch，进程与部署匹配。冻结/解冻跨进程恢复未完成，旧实现结果不转记为active通过。
- .35 中心盘约137GiB，正式中心200GiB门保持；备用 .39 仅完成工程样本传输核验。

这些数字只表示当时进度。当前阶段不报告 G0/G1 闭合、正式训练启动或搜索提高实力。

## 历史实验

保留了 `_spec_work/evidence/` 中价值训练、教师采样、学生消费、连续训练及教师评测的 README。它们属于旧协议背景，部分引用的私有原始附件未公开；不代替新版 G2/G3 验收。

## 后续验收顺序

G0/G1 剩余正确性与完整局 → G2 正式迁移及 WDL 校准 → G3 搜索增益 → 后续训练和模型发布门。每阶段证据完成后再更新本页。
