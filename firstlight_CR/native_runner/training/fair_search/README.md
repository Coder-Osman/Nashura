# FAIR native 验证入口

固定每个决策根 32 次模拟。完整 native 终局、无协议拒绝和无未确认执行后，才原子发布整局 `SearchEpisode`。失败、超时和诊断根均不进入训练回放池。

## 价值头迁移初始化

新启动默认从1755迁移旧价值头，不再将搜索价值头置零。保留旧头的隐藏层及 SiLU，
把标量输出 `u` 的末层权重映射为胜／平／负 logits `[u/2, 0, -u/2]`。
这保留旧估值的正负和排序，搜索值仍为 `P(胜)-P(负)`，不直接使用无界PPO标量。
迁移层独立存储；旧策略权重不变，优化器与回放池重新初始化。

旧模型使用圣水溢出及塔血塑形；迁移输出只是初始化，不宣称已经校准的胜率。
后续仍只用真实终局胜／平／负类别训练，不引入旧奖励、PPO损失或旧优化器状态。

```sh
python -m native_runner.training.fair_search.initialize \
  --parent /tmp/fair-opt-20261009/checkpoint.pt \
  --output /tmp/fair-value-transfer-20261010/initial.pt \
  --particles 1 --device cuda:2
```

初始化文件采用 `fair-search-checkpoint.v2`，显式记录 `value_head` 结构。
文件SHA作为新模型身份，1755的SHA只作为父权重身份；旧零价值局不会冒充迁移模型数据。
初始化入口拒绝覆盖已有文件。`bootstrap()` 返回的SHA仍表示父PPO文件，
正式自博弈应使用保存后的搜索checkpoint SHA。

`smoke --checkpoint` 可直接读取搜索checkpoint，并检查搜索配置匹配。
若传入已校验的1755原文件，会先在输出目录保存 `initial-checkpoint.pt`，再用新文件SHA采集。
已有 `fair-search-checkpoint.v1` 继续按原线性头加载，保留原预测及优化器对应关系；
加载旧文件不会自动迁移或重新初始化。旧模型需要显式作为基线，不替换新初始化。

根测速入口若接收1755父文件，会在首个试验前生成一次正式初始化文件；
所有交替A/B试验共享该文件及模型SHA，避免重复初始化或跨模型比较。

## 历史共享与条件快照

信息历史使用不可变的 32 条记录分块与增量 SHA 链。提交只追加新记录，根捕获与恢复共享历史前缀；分支回滚只撤销新增公开事件。完整语义帧和 native 状态 SHA 校验仍保留。信息哈希契约为 `fair-information-history-chain.v2`，整局与 manifest 使用 v2；旧 v1 整局显式拒绝，避免把旧哈希静默解释为新格式。

`--conditional-draws` 是独立启用的原生验证选项，要求包含 `belief-draw-probe` 的已绑定产物及 compact transport。默认关闭，旧流程仍可作为对照。它枚举固定卡组的兼容初始牌序，并仅用新增己方记录、公开执行和当前己方可见手牌筛选。枚举上限为对手 8!，不会随比赛时长增长；它不读取实际比赛对手牌序。

既有世界不再兼容新根时，先恢复上一根，再根据条件牌序更新采样世界的手牌和循环队列，只重放两根之间的增量。原生命令核对比赛代次、状态代次、tick、原队列、固定速猪基础卡组和补牌空槽数量，全部通过才修改队列；不修改战斗实体、卡牌对象或随机状态。更新后先验证原根己方信息未变，再验证目标根的完整 FAIR SHA。失败关闭该次尝试，不通过整局重建掩盖条件更新错误。

补牌模型保留原生临时空槽和阶段相关间隔。已绑定的 Ladder 引擎在可观测 tick ≤2400、2401–4800、≥4801 补牌时，分别设定下一次补牌最早时间为当前 tick 加 20、10、7；阶段切换不追溯缩短已设定的截止时间，空闲后首次补牌按实际发生 tick 重新计时。边界使用步进后的观测 tick，原生内部事件时间比执行收据早一个 tick。该规则仅适用于当前验证过的固定模式与引擎。模拟对手的全部决策仍保留，按最初四次补牌划分为共享历史段；条件更新只重绑定已观测抽牌，未揭示的初始牌不进入信息身份。第四次补牌后的抽牌由公开出牌顺序确定。该模拟历史可用于推理传输，但训练决策契约显式拒收它；实际比赛己方历史保持原始记录。

设置 `--diagnostic-directory` 后，增量更新失败会原子保存 `update-root-OWNER-TICK-ATTEMPT.pt`，包含前后 FAIR 信息状态、独立世界 seed、条件排列与补牌截止时间。文件按尝试隔离，不读取真实对手私有状态、不发布训练数据；原有根 SHA 和完整预算检查保持严格。

该路径的实验产物、诊断轨迹和验收状态见 `VALIDATION_20261009.md`。根局部优化不代表整局或训练吞吐同比提升。

## 已修复的公开历史回放

公开出牌在其可见执行 tick 的前一个 tick，使用采样世界中的真实手牌槽，通过 `BattleEnvV1.step` 提交。正常执行收据负责公开事件、圣水 tracker 和语义帧更新。模拟世界的手牌、规则合法性、执行结果及完整根信息哈希仍需通过校验。

直接调用 native replay 调度接口会执行出牌，但绕过模型动作收据路径，造成公开历史和语义帧不一致。此入口不手工补入期望事件，不强制制造 ready，也不推进到根之后来匹配目标状态。

条件初始化只使用己方已知抽牌前缀和对手首个已公开出牌。对手首牌从四个初始手牌槽随机选择一个放入，其余排列独立随机生成；最终仍由完整合法回放筛选。这是近似信念采样。

己方前缀同时读取已提交决策中的抽牌历史与当前 FAIR frame 的 next-card 标记。出牌后的新 next card 在下一次决策提交前就已可见，必须在当前根初始化时纳入条件。

实际比赛、公开历史重放和搜索分支统一逐 native tick 推进。FAIR 伤害事件来自相邻观测的 HP 差值；跨 tick 聚合会改变事件金额、数量和时间，且推进边界可能依赖对手未公开计划。统一观测节奏后，事件帧不再依赖这些隐藏边界。完整根哈希与快照恢复校验仍严格执行，旧批量推进轨迹保留为诊断证据。

运行时身份包含 PUCT rollout、FAIR native adapter、belief provider、transport、native 控制客户端、resident batch channel 和 compact decoder 的源码哈希，以及所选 transport，区分推进、信念重建和遥测语义的更新。

每 tick 的公开出牌历史直接读取正常执行收据，避免为扫描事件重新生成双方观测；完整观测仍在决策边界正常生成。收据保持相同的可见性过滤和事件去重，原生推进继续逐 tick。

信念世界快照同时保存 native、rollout 和完整 `BattleEnvSidecar`。每次模拟恢复事件环、执行队列、HP 差分基准、FAIR tracker 关联状态与缓存，再按 native 恢复收据重绑定事件代次。仅恢复 native 和 rollout 会让上一分支的 Python 事件混入下一分支；此类旧运行保留为诊断，训练前需隔离。

游标重绑定要求恢复 tick 和代次一致、日志完整、序号连续且没有溢出或拒绝记录。combat、visibility、remaining 必须为空；phase 允许恢复观测本身产生的根 tick `movement_scale` 测量记录作为新基准，其他事件仍拒绝。

对手公开出牌序列用于采样牌序的预检查：按四张手牌、四张循环队列执行公开卡牌消费，必然缺牌的排列在 native 重放前拒绝。此检查不增加 RNG 消耗、不重采样或调整尝试预算，因此固定 RNG 下保留原先相同的可接受世界。最终仍以真实规则重放、完整 FAIR 帧与快照校验作为兼容门槛。

## 单局验证

在已校验的远端工程及引擎环境运行：

```sh
python -m native_runner.training.fair_search.smoke \
  --checkpoint /tmp/fair-search-rl1755.pt \
  --puct-root /tmp/firstlight-v2-p11-candidate/research/puct \
  --main-port 30045 --belief-port 30046 --opponent-belief-port 30047 --particles 1 \
  --belief-reserve-worlds 0 \
  --belief-attempts 32 --threads 1 --search-only --progress \
  --diagnostic-directory /tmp/fair-replay-diagnostics-20261008 \
  --output /tmp/fair-smoke-20261008 --seed 1755 --max-seconds 7200
```

`--progress` 为每个完成的根输出 tick、所属玩家、32 次访问、粒子数、分阶段耗时和信念复用/失效统计。诊断目录保存失败根的允许信息字段及独立采样 RNG，不保存真实对手手牌、比赛 seed 或隐藏计划。

默认 `--belief-mode incremental` 为双方分别保留独立采样世界及完整根快照。下一根恢复此前的世界，仅重放新增加的己方决策和公开事件，完整 FAIR 帧仍必须相同。默认 `--belief-reserve-worlds 0`；显式设置 `1` 可试验备用池，每方需要 `particles + reserve` 个独占 belief 端口，总和不超过 8；备用世界只跟随根更新，不参与搜索采样。上面的三端口示例显式关闭备用池；启用一个备用世界时需为双方各增加一个独占 belief 端口。活动世界被新公开信息排除时，兼容备用世界直接接替，避免在该根为这个活动位从开局重建。备用世界失效时不在当前根同步补建；兼容世界总数不足以填满活动位时，才尝试重建缺少的活动世界。重建失败但仍有兼容世界时允许少量世界继续。`--belief-mode replay` 保留从开局重建的对照路径。

未启用 `--conditional-draws` 时，新可见抽牌或对手公开出牌可能排除此前世界，此时严格丢弃并重新采样、合法重放；增量模式不是跳过兼容校验。正常复用的开销取决于两根之间新增的时间和当前局面。进度事件和最终运行汇总记录 `reserve_reused`、`reserve_invalidated` 与 `rebuild_avoided`，计数单位是被替代的世界，不代表整个根没有重建。进度事件还记录当前 `active_worlds` 和 `reserve_worlds`，以及条件更新次数 `conditioned_roots` 和累计耗时 `conditioned_seconds`。备用池不持续补充，可能在早期耗尽，且增加首根初始化和后续维护成本；启用备用池不保证消除后段重建长尾或提高整局吞吐，需独立对照验证。

`--search-only` 使用 PUCT runtime 的搜索专用提交与快照接口，跳过模拟分支的 PPO 记录和重复动作概率评估；训练标签仍由 FAIR collector 根据完整终局生成。未启用时使用旧记录路径作为性能与语义基线。

真实比赛默认也使用轻量执行账本，直接提交搜索已选出的动作，保留合法性校验、延迟、取消和执行回执，不再为提交动作编码模型或写旧 PPO 记录。`--record-policy-steps` 恢复真实比赛的旧动作评估和记录路径，供隔离对照；FAIR 训练帧仍由 collector 独立保存。

默认 `--device cpu`、单线程 FP32。`--device cuda:0` 可显式选择 GPU；它仍使用 FP32，不启用自动混合精度。CPU 和 GPU 的结果另行比较，并在输出中记录设备。

默认 `--transport compact` 使用独占 resident slot 0 的压缩训练捕获通道，每次推进一个 native tick，同时返回普通状态与严格解码的 typed rich snapshot。同 tick 的后续观测复用该捕获，减少单独 JSON 请求和重复字段校验。初始化预热沿用原控制通道，reset 完成后启用单 tick 原子推进；注入动作、恢复和其他控制请求先关闭 batch 通道并清除捕获缓存。快照控制额外验证只有 slot 0 占用引擎，防止全局控制作用于其他比赛。

显式 `--transport compact-delta` 使用新增 V5 捕获协议，只传输上次已校验序号之后的 combat 记录。游标绑定比赛 generation 和 state epoch；动作控制导致连接重开时保留游标，恢复或重置后的新 epoch 必须重新校验完整新前缀。序号缺口、重复、截断、错误代次与部署上下文变化均拒绝，不回退到 V4/JSON 或伪造历史。该选项要求对应新 native 产物，并继续严格验证引擎身份。

`--transport json` 保留普通 JSON 路径作为对照；默认 compact 保持 V4 兼容并接收完整 combat 窗口。phase 数据原本已是当前状态快照，V5 不改变它。差量传输不清空游戏历史，不放松事件完整性或 native/Python 恢复校验。运行中的旧局保持原源码，新通道使用独立目录和端口。

引擎进入 resident 模式后会拒绝旧式无 slot 前缀的 JSON 配置。JSON/compact 交替对照应使用相同引擎产物启动的两个独占端口，分别固定一种 transport；保持根、模型、RNG、32 次预算及信息轨迹一致，避免在同一个进程内切换引擎模式。

## 单根复现和交替 A/B

2026-10-10 的状态优化使用配套 PUCT bundle
`_spec_work/runtime-speed-20261010/`，远端独立验证路径为
`/tmp/fair-state-opt-20261010/project/research/puct-optimized`。
该 bundle 的 `runtime/` 是实际目录，部署时须保留层级，避免旧绝对符号链接导入旧 helper。
项目的 `event_set.py`、`battle_env.py` 和 FAIR native adapter 需与 bundle 一起部署。

搜索首次捕获 tracker 时，将已验证事件身份转换成结构共享集合；后续捕获和恢复只复制根引用，
新增事件只复制哈希路径，分支之间保持独立。旧 PPO tracker 默认仍用普通集合。
JSON 快照沿用原 `set` 标签，语义摘要保持一致；完整导出和摘要仍需要遍历数据。
塔身份使用 reset 时绑定的反向索引，仍检查归属、坐标和最大血量。
纯几何放置缓存跨恢复保留，键包含卡牌、玩家、形态、塔和建筑占位等几何输入，
并绑定环境规则与卡牌目录；动态运行时缓存、事件游标及执行计划仍正常回滚。
运行时身份另外包含实际加载的 helper、tracker、集合实现、BattleEnv 和摘要代码 SHA。

相对上轮推理优化版，开局与后段各三轮交替 A/B 的稳态根中位数分别为
15.873→15.083秒、20.375→19.360秒，均约1.05倍；后段恢复阶段4.267→3.538秒。
两侧32次访问、动作和分支轨迹一致。这些数字排除冷信念重建，不代表整局吞吐。
原始数据和限制见 `_spec_work/evidence/fair-search-20261010/state-optimization/VALIDATION.md`。

推理默认使用按模型 SHA 和完整 FAIR 信息 SHA 隔离的 LRU 编码缓存，最多64条、
256MiB张量；检查模型参数、buffer、设备、dtype及训练模式，权重变化后拒绝继续使用。
缓存包含编码、价值和已评分动作，不缓存抽样动作或按 seed 固定候选集。
候选生成仍消耗相同的请求局部随机数；只在一次请求内复用相同条件的采样分布，
省去训练用熵计算。最多32个候选一起评分，仍使用原完整合法空间的自回归联合概率。
训练损失和32次顺序模拟保持原定义。

显式参考路径：`--inference-cache-entries 0 --inference-score-batch 1 --reference-sampling`。
本地推理的逐根日志包含累计 `inference_profile`；相邻日志差值用于估算每根命中率和阶段耗时。
GPU合批可能产生浮点舍入差异，验收同时核对候选、访问分布、所选动作及分支信息轨迹。

多actor可连接同一GPU上的冻结服务，默认最多32请求、等待2ms、有界队列128：

```sh
python -m native_runner.training.fair_search.serve_inference \
  --checkpoint "/path/to/search-checkpoint.pt" \
  --directory "/tmp/fair-inference-run-unique" --device cuda:0
```

各actor的 `smoke` 命令增加 `--inference-socket "/tmp/fair-inference-run-unique/inference.sock"`。
各actor仍使用独占比赛和信念引擎；服务按checkpoint SHA分批，不在请求中切换权重。
`--checkpoint` 可重复指定冻结的历史模型。SIGTERM/SIGINT关闭服务并输出合批统计。
外部服务模式下，采集器默认将本地模型留在 CPU，供条件世界重放使用模型配置和合法性元数据；搜索推理由 socket 服务执行。`--device` 选择服务外的本地推理设备；使用 `--record-policy-steps` 时，本地模型也按该设备加载，以复现旧路径。运行身份记录实际本地设备及提交模式。
服务不改变同一棵树的顺序访问；单actor同步请求通常凑不满跨actor批次。
外部服务模式不接受本地推理调优开关，服务设置由启动命令负责。


给 smoke 添加 `--replay-root PATH` 可直接重建失败根，不重复此前整局搜索；添加 `--search-root` 会在该根完成 32 次评测搜索。结果包含完整根哈希、稀疏访问计数、选中动作和分支信息轨迹哈希。

```sh
python -m native_runner.training.fair_search.benchmark_roots \
  --checkpoint /tmp/fair-search-rl1755.pt \
  --puct-root /tmp/firstlight-v2-p11-candidate/research/puct \
  --replay-root /tmp/fair-replay-diagnostics-20261008/replay-root-0-393-0.pt \
  --main-port 30045 --belief-port 30046 \
  --output /tmp/fair-root-ab-20261008 --pairs 3
```

基准串行交替运行基线和搜索专用路径，逐对比较模型、搜索配置、runtime、根信息、分支信息、访问计数及选中动作。任意差异都中止验收；仅一致时报告重复试验中位数及根加速倍率。每次基准使用新输出目录，保留各次原始结果。

事件批量推进曾产生相同根访问计数，但分支信息轨迹不同，因此已移除。初始中性价值头下，相同访问数不足以证明模拟语义一致。

根倍率不代表完整对局吞吐。完整单局、多粒子、actor 容量和五机代际训练需要后续独立验收。
