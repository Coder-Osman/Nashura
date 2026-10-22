# 完整Full128搜索局与跨进程恢复：工程验证口径

核验日期：2026-10-21。入口：
`firstlight_CR/tools/probe_fair_final_search_episode.py`；独立部署与清理：
`run_search_episode.py`、`search_episode_worker.py`；只读状态：`inspect_search_episode.py`。

## 固定范围

- .35单actor，实际比赛与独立信念各一个引擎，端口29731/29732；不启动五机任务。
- 冻结来源A：e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015。
  这是来源适配器工程验证，不是已通过G2的正式教师。
- 独立新版phase-snapshot引擎SHA：
  49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba。
- 双方所有根均Full128、root_noise=0、默认完成边界时域；
  搜索、执行采样与实际比赛私有seed分离，行为按原始访问分布采样。
- 同tick先冻结双方视图，再各自搜索，最后统一提交；每根核验实际引擎摘要和
  全部冻结FAIR视图未变。根允许对手已有pending，但己方须在完成决策边界。
- 使用RefillPriorHistoryBeliefFactory与PriorHistorySearchProvider；
  一般pending根通过合法历史和独立初始化重建，不复用仅限双方ready的局部重条件化器。

## 恢复与完整性

每组搜索先原子保存FAIR帧、合法mask、身份、稀疏访问和执行选择，再提交预约。
严格校验128有效访问、未完成边回传为0、标签与访问完全一致、执行动作合法且有访问。
正常抢占保留记录；拒绝、截断、恢复差异或无兼容世界立即失败，不填补访问数。

prepare进程提交3组决策，在下一决策边界保存未提交的Full128见证与私有日志头，
随后退出。新resume进程重建实际比赛、FAIR历史和已提交actor记录，重新搜索见证根；
完整结果、候选访问、目标和执行动作须严格一致，之后继续同一逻辑对局。
这验证进程重启后的日志重演和搜索快照一致性，不声称原生进程热迁移或任意指令点崩溃恢复。

只有原生完整终局、每个决策与预约结算一一对应、冻结模型不变时才生成
complete-report.json。worker还须证明所属两个引擎退出、端口释放。
每阶段最长6小时，内存余量32GiB、工程磁盘余量5GiB保护；失败不自动新开一局。

## 证据状态与限制

专项及相邻搜索/恢复回归66项通过，见search-episode-engineering-tests.xml。
其中新增trace测试11项；这些内存fixture与持久化测试不替代原生完整搜索局。
不同测试范围不与先前111项或34个局部门PASS相加。

原生attempt：native-search-episode-1792552718166807000，已在tick1094超时失败并清理；
launch.json冻结部署源码、上传载荷、引擎和进程身份。运行进度保存于该目录status-*。
已有跨进程搜索恢复见证的内部通过报告，尚无完整局、独立收尾验收或清理通过结论，
G0/G1整体仍IN_PROGRESS。
2026-10-21补充：原局未形成终局，独立完整验收拒收。55条日志及27组54根
6912次有效模拟已核验；最后提交tick1066，下一边界tick1094。
独立continuation-v1已在新进程核对该边界的native/clock/evidence/FAIR并继续搜索。
原失败文件保留，续跑身份、六小时分段、归档及最终验收见
_spec_work/versions/fair-final-search-continuation-v1/CONTRACT.md。
最新快照1792555613.409947：tick90/150/152双方共6根、768有效模拟，三组已提交；
各根耗时296.81/269.46/229.02/177.17/642.17/638.15秒，当前tick212玩家0
正在计算下一组未提交恢复见证。完整G0/G1剩余条件见G0_G1_CLOSURE_CHECKLIST.md。

更新快照1792558218.4345498：prepare在tick212完成checkpoint；新resume
进程对双方各重算128次有效模拟，recovery-search-report.json为passed，
耗时568.77/558.40秒。冻结探针内部比较候选、访问、目标及选择一致；第4组
随后提交，tick272玩家0完成128访问、玩家1计算中。这不是第二次独立原生
验收，也不替代完整局、actor记录全程核验及所属引擎清理。

独立history-v2工程副本补充拒绝证据契约，不改本attempt的244个冻结源码文件。
该副本已通过完整策略混合proposal修补隐藏拒绝的冷重建fixture支持，未原生
验收、未部署至本任务；旧attempt的
任何结果只证明launch.json绑定版本，不证明新副本的一般pending正确性。
快照1792558392.198128：tick272双方各128访问完成，第5组提交，正在计算
tick332玩家0；无完整终局、最终独立验收或清理报告。独立v2专项21通过、
最终回归633通过/2跳过只归属v2身份，不叠加到本attempt的原生证据。

更新快照1792559080.7465053：已提交6组，tick362玩家0完成128访问、248.20秒，
玩家1计算中，仍无完整终局与清理报告。独立v2新proposal专项88通过、完整回归
647通过/2跳过，绑定新sampler与version-manifest；旧21/633报告及其manifest另存
before-full-support身份。新旧版本不混用，也不把新fixture测试计入本原生任务。

独立收尾入口collect_search_episode.py仅在远端completion.json存在后收集证据；
逐文件核对SHA及长度，不清理远端数据。仍在运行时仅返回running，不颁发通过。
verify_search_episode.py核对部署载荷、当前源码、完整终局标签、持久化每根搜索、
预约执行/抢占时序、actor提交记录、恢复日志链与外部固定日志头，以及所属引擎清理。
它不重新运行原生恢复；恢复搜索结果相等仍由冻结源码中的比较断言和恢复报告证明，
不将文件校验宣称为第二次独立原生重演。
验收器30项fixture测试通过，6.99秒（search-episode-evidence-tests.xml），包括不完整局、
坏数据/丢失预约、篡改日志及重算哈希后仍与actor不符的证据拒收；不替代原生通过。
逐玩家上一预约的结算tick必须等于下一根tick；各组pending必须与跨越该tick的
未结算预约一致。抢占必须有同tick公开执行证据，公开执行后的决策组冻结双方。
最后双方预约都在同一游戏动作关闭tick结算；该tick可以早于native finalized终局，
但同tick到期仍先执行/WAIT到期，只有尚未到期的预约才允许terminal_expired。
覆盖游戏关闭早于/等于finalized、合法公开抢占以及伪抢占/遗漏决策边界拒收。
本轮仅修改独立文件验收器及其fixture，保持运行中的244个生产源码文件不变。

收尾命令：在工作区根目录使用firstlight_CR/.venv-live/bin/python运行
_spec_work/evidence/fair-final-v1-20261019/collect_search_episode.py，参数为该attempt目录。
成功只生成限定工程verification.json，不修改gate-status.json或开放正式训练。

所有产物training_admissible=false；不进入Replay，不更新权重，不发布模型。
不证明经验先验总体校准、所有隐藏机制非干扰、正式迁移保真或搜索实战增益。
当前每根冷重建历史，可能随后段历史增长；本轮优先验证正确性，不宣称恒定重建成本。
中心200GiB门槛仍未通过，正式迁移与训练继续关闭。

2026-10-21 continuation轮询补充：独立续跑已提交31组、62根，累计7936次有效模拟；
tick1126双方128访问已完成，下一根为tick1128 owner0。worker43008、搜索进程43118
及29751/29752所属引擎仍存活，尚无segment、终局或失败标记。该快照只更新运行进度，
不改变原失败前缀、完整局验收待定及`training_admissible=false`结论。
