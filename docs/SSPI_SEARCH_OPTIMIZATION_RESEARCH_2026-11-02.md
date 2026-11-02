# SSPI 搜索剩余优化空间调查

## 结论

最新已发布的单根实测中，128 次有效模拟耗时中位数为 24.675 秒。最大顶层阶段是 `transition`，18.272 秒；其次是模型推理 2.007 秒、信念构造 1.809 秒、恢复 1.327 秒、候选生成 0.666 秒。PUCT 选择仅 8 毫秒，当前不是值得优先投入的热点。

`transition` 占根墙钟约 74.0%。已计入的互斥顶层阶段合计约 24.089 秒，距 24.675 秒根中位数约 0.586 秒，包含尚未分类工作与汇总口径差异。`step` 与 `observe-atomic` 两类请求各自的中位累计耗时相加约 6.227 秒；这是同步 RPC 总时长而非纯 TCP/JSON 时间，也仍包含 native 推进/捕获，纯网络节省应另测。它们之外的 transition 工作尚未在缓存优化后的源码上拆分，尚不能归因到 Python、对手推理或原生模拟。

更关键的发现是，性能探针走的 belief-world 路径仍逐 tick 请求原生引擎和原子观测。每根累计 2,812 次真实 tick 推进、2,813 次 `step` 和 2,943 次 `observe-atomic`。优化后的 RPC 累计计时中，`step` 为 2.961 秒，`observe-atomic` 为 3.267 秒；这些是嵌套诊断，不能叠加到 `transition` 阶段。当前最有希望的工作是将现有原生事件批量捕获接入 MCTS 的 belief worlds，并保持每个决策边界和搜索输出完全等价。

本次为源码与已发布证据调查，没有修改训练代码，也没有运行新的原生搜索。当前 AVD/端点未执行状态查询或重配；因此本文不声称新路径已经提速。

## 证据边界

优化前后受控对照使用相同模型 SHA、root FAIR SHA、随机种子和 persistent `session-v1` 传输；墙钟中位数从 57.588 秒降至 24.675 秒，32 项候选访问分布、所选动作、2,812 个 native tick、RPC 次数和快照恢复检查一致。该结论只覆盖一个固定开局 root。

之前的函数级剖析报告来自静态编码与摘要缓存实施前的源码。它曾把大量时间归因于 MechanicProfile 编码、FAIR 摘要和对手策略调用；最近优化后不能把这些旧子项比例直接套用到 24.675 秒的新基线上。最新对照只公开了互斥顶层阶段和嵌套 RPC 计时，没有缓存优化后的 `transition` 函数级拆分。

## 代码路径发现

1. 真实对局采集在 `transport=event-json` 时使用 `EventBatchedSSPINative`。此客户端启用 `step_until_event=True`，由原生 resident worker 在公共部署、原生终局或请求 tick 预算处停止，并返回该边界的原子捕获。
2. 同一采集器为 MCTS 创建 belief provider 时仍使用普通 `SearchNative`；已发布的单根性能探针也使用 `LegacySearchNative`。`FinalDecisionRuntime.advance()` 仅在环境声明支持事件批量推进时才跨多个 tick，否则每次只推进一个 tick。因此真实对局的事件批量路径并未自动覆盖 MCTS 分支。
3. 现有调度器已能按下一次决策预约和可信补牌到达时刻限制推进范围；SSPI 使用 `wake_refills=False`，补牌到位不是额外决策。事件批量推进仍逐 tick 在 native 内执行战斗，只会减少宿主请求/捕获频率，native tick 数和游戏时域均保持原样。resident 的 `step_until_event` 实现提供了候选实现基础，belief-world 集成仍需 FAIR 等价性验收。
4. 直接复用 `EventBatchedSSPINative` 还缺少搜索快照生命周期适配：`HistoryBeliefFactory` 当前在重置后调用不带 `initial_tick` 的 `start_atomic_steps()`，而批量客户端需要初始 tick 建立边界；客户端 restore 会清 JSON 事件游标，但其本地 `_last_tick` 也需要从恢复观测重新定基。否则分支恢复后可能出现错误 tick 间隔或边界历史。
5. 通道层允许同时请求 `step_until_event` 和 compact `training_capture`，但 SSPI 事件客户端当前假定 capture 是 JSON mapping；compact 路径返回 `TrainingAtomicCaptureV1`，还需适配 ordinary/rich 捕获、事件增量游标和 restore 失效流程。配置开关单独不足以完成适配。
6. `FinalRollout.freeze_due_information()` 和 `RolloutSnapshot` 会复制/打包模型帧、合法性及 tracker 状态。`belief` 阶段中独立重建/历史回放、快照复制和 `freeze_due_information()` 也没有缓存优化后的互斥拆分；先加不增加原生请求的函数级计时，再判断是否值得改动。

代码库的相邻 FAIR 搜索入口已有 `CompactSearchNative`，采用 resident compact typed capture、严格解码和逐 tick 推进；这不是当前 SSPI belief-world 已接入的能力。在 2026-10-09 的不同版本固定根上，三组逐 tick 对照的搜索根中位数为 147.430→61.516 秒（约 2.40 倍），双方都推进 1,059 ticks，86 个信息帧和搜索输出一致。它证明 compact transport 值得在当前根重新测量；不同版本、状态与剖析配置下，该倍率只作可行性参考，不用于预测当前 24.675 秒基线。证据见[历史 FAIR 捕获路径验证](../firstlight_CR/native_runner/training/fair_search/VALIDATION_20261009.md)。

## 优化机会排序

| 优先级 | 方向 | 证据与预期 | 必要验证 |
| --- | --- | --- | --- |
| P1 | 将 event-batched/atomic capture 接入独立 MCTS belief worker | 当前根仍执行 2,812 个 native tick、2,813 次 `step` 与 2,943 次原子观测；native 原语和 actor 客户端已存在。它减少宿主往返，不减少 native 模拟。潜在收益高，但本调查没有新测速，暂不估算倍率。 | 先只读确认单槽 resident 能力与引擎/probe 身份；恢复后重设 tick/游标；同一根上比较完整 128 次搜索；FAIR SHA、每个决策/公共部署 tick、候选与访问分布、动作、有效模拟数、native root digest、补牌状态、句柄数逐项一致。记录 batch 数、capture bytes、实际 tick 和墙钟。 |
| P2 | 拆分 belief 与 Python 快照/帧复制成本 | `belief` 顶层 1.809 秒，`restore` 1.327 秒。每根有 213 次 snapshot-create/release、130 次 restore；`freeze_due_information()` 对冻结帧做 pack/unpack 与 legality deepcopy。当前记录未区分重建/历史回放和本地对象复制的耗时。 | 只增加宿主单调时钟计数，不增加 native 请求；分别计时 belief session sync、历史推进、freeze、snapshot Python/sidecar、tensorize。再基于比例决定是否消除不可变数据的重复复制，验证张量、FAIR SHA、RNG、状态恢复与信息隔离。 |
| P3 | 评估 resident compact capture 与事件批量组合 | `ResidentBatchChannelV1` 支持 event-stop 和 typed compact capture 的底层选项；相邻 FAIR 搜索已有逐 tick compact 客户端和真实固定根等价结果，但当前 SSPI event-batched 客户端仍使用完整 JSON。组合可能减少传输/解码开销，尚无当前 SSPI belief-world 证明。 | 先复用/适配 typed `TrainingAtomicCaptureV1` 并记录恢复后 decoder 行为；再与 P1 的 JSON event-batch 变体做同根对照。保持完整 rich 校验，逐项比对 FAIR/轨迹/访问分布，并报告传输字节、解码时间和端到端墙钟。 |
| P4 | 测量冻结模型的重复信息帧推理命中率，再考虑 root-local policy cache | 顶层推理 2.007 秒；对手采样及其推理嵌套在 transition 中，新基线未独立分离。相同 FAIR 信息若重复出现，缓存可能节省推理；目前没有重复率证据。 | 记录完整信息身份的重复次数/唯一数；命中率不显著则不实现。若实现，缓存键绑定 model/controller/device 与完整 FAIR SHA，仍用每次原始 RNG 采样，验证策略分布和 RNG 后态。 |
| P5 | 合并 snapshot restore 后的状态捕获请求 | 每根有 130 次 restore 和 278 次普通 observe；恢复返回后额外获取观测可能可合并。相对 transition 总耗时较小，需核对引擎协议支持。 | 单独比较 restore receipt、FAIR 状态和 rich telemetry 原子一致性，以及失败恢复和游标重建。 |

当前不优先优化 PUCT 选择（约 0.008 秒），也不应因旧开局 root 的 JSON delta 响应仅减少约 2.72% 而重做增量序列化。CPU/MPS 推理比较可以等 transition 瓶颈收敛后再做；最新 top-level inference 约占根墙钟 8%，且对手推理的嵌套成本尚未重测。模型规模、搜索预算、搜索时域、FAIR 校验与原生战斗逻辑都不作为提速旋钮。

## Transition 深入分析

`CompletedBoundarySearch` 对每次 `world.transition(...)` 包围计时；128 次有效模拟会经过多条树边，所以 18.272 秒是这些调用的累计值，不是单条边的时长。PUCT 选动作在计时器外；但 transition 内含对手策略推理。搜索报告里的 top-level `inference=2.007s` 只统计搜索方根/叶预测，不含 `opponent.sample()` 内部的预测。

| 内部阶段 | 具体工作 | 最新根证据与判断 |
| --- | --- | --- |
| 边前快照 | 复制 tracker、RNG、冻结信息和 Python sidecar，并在原生 worker 创建 snapshot handle；退出边时释放 handle。 | 每个根有 213 次 create/release；实现中每条边都建局部快照。它保护不完整边的回滚，但成功边不会恢复该局部快照。 |
| 同时决策提交 | 冻结双方当前 FAIR 信息、合法性检查；根方采用搜索候选，对手策略按它自己的 FAIR 输入采样；两边动作一起写入 DecisionClock。 | 对手预测属于 transition，而非搜索器单独的 `inference` 阶段。历史 profile 中 212 次对手 sample 累计 13.566 秒，属于模型缓存优化前的数据，只能作为“需要重新分项计时”的证据。 |
| 预约与原生推进 | PLAY 做手牌/费用/坐标校验，队列预约并核对原生回执；WAIT 或预约等待推进到下一观察边界。 | 当前 belief worker 用普通 `SearchNative`，所以每个 native tick 发 `step`，随后发原子观测。每根 2,812 ticks 对应 2,813 次 step 和 2,943 次 observe-atomic。 |
| 捕获与 Python 状态推进 | 解码 ordinary/rich 捕获，合并并校验遥测，更新事件、补牌估计和决策时钟；仅在有效边界建立策略信息。 | 同根 step 与 observe-atomic 的嵌套累计 RPC 时间约 2.961 + 3.267 秒。它们含 native 执行/捕获，不能等同纯网络成本或从 transition 中直接扣除。 |
| 边界判定与回传 | 搜索方到达下一决策点时返回新 FAIR 信息；终局时返回根方终局值；未完成边按当前接口恢复边前快照。 | 所有阶段仍须保留完整执行收据、对手隐私、终局标签和完整 128 次有效模拟。 |

批量接入还有一项容易遗漏的连接生命周期成本。`EventBatchedSSPINative._request()` 在**每次**控制请求前关闭 batch 通道，包括 `snapshot-create`、`release-snapshot`、`restore` 和出牌预约；下一次 `step()` 再新建通道。底层 `ResidentBatchChannelV1` 注明 native 控制服务器为单客户端，因此不能未经验证让控制与 batch 两条连接同时保持打开。在当前每根约 212 条边、213 对快照请求的搜索形态下，直接替换客户端会频繁重新握手；应记录 `connect_ns`、通道新建次数及每个 batch 的平均 native ticks。若连接开销吞掉了合并观测的收益，应先单独处理成功边局部快照或合并原生动作与推进，而不是调整 TCP 参数。Batch 协议本身支持在一次 step 帧中带 PLAY 动作并返回回执，但当前 FinalBattleEnv 还校验普通预约回执中的额外卡牌/时间字段，两条路径需逐字段证明等价后才能合并。

另一个边界是补牌。SSPI 不把补牌到位作为新决策，但 `HogRefillClock.next_boundary()` 会在已知补牌预计 tick 限制下一次批量跨度，以观测核对实际到位与连续补牌。因而 event batching 目标是减少**无决策、无公共部署、无待核对补牌**区间的中间捕获，而不是机械地将 2,812 次逐 tick 观测全部压到 212 次边终点。已知补牌边界和原生终局仍要保留。

逐 tick 原子捕获也不等于每 tick 都构造完整模型输入。`FinalDecisionRuntime._freeze()` 只有在时钟唤醒 owner 时才调用 `observe_dual()`；旧函数级剖析记录 2,812 次 advance 对应 213 次 `observe_dual()` 和 426 次 tensorizer 调用。新版本未重新测这些子项耗时，但这个调用结构表明应优先减少无决策区间的 native 捕获，而不是假设 2,812 次完整张量重建。时钟证据扫描、每 tick 补牌 hand-set 比较、行动回执校验等 Python 工作仍需分项计时后再决定是否优化。

恢复也解释了部分额外捕获：原生 `restore()` 先请求恢复，再读取普通 `observe`；`FinalSnapshot.restore()` 为重建 rich 游标又读取原子观测。最新根的 2,943 次 `observe-atomic` 比 2,813 次 `step` 多 130 次，恰好等于 130 次 restore。合并这两次恢复后读取可能节省请求，但须保持普通状态、rich 代次和 sidecar 重定基一致；它是独立于逐 tick 事件批量的优化项。

最新根中 discarded edge 为 0，而 native 快照计数为 213 次创建、213 次释放；对应 1 个 belief-world 根快照和约 212 个 transition 局部快照。没有一条 transition 需要局部回滚。仅原生 create/release RPC 的累计中位数约为 0.245 + 0.251 = **0.496 秒/根**，还未计入 Python sidecar 与状态复制。可单独测试让不完整边标记当前 world 为 dirty，由搜索器丢弃该模拟并在下一模拟开始时恢复根快照；这样不需保存每条成功边的局部快照。此调整会改变 transition 的失败后状态契约，必须明确让调用方跳过“恢复到边前状态”的断言，并验证 incomplete 后的根恢复、末次模拟退出清理及异常关闭。预期收益小于事件批量，但计数证据直接、变量容易隔离。

transition 内部最大的未知项仍是优化后对手模型预测占比和 native/Python advance 的当前分配。静态编码缓存已显著降低根/叶推理；对手调用也会受益，但最新探针没有给 `opponent.sample()` 单独计时，因此历史 13.566 秒不可直接缩放。应先记录每根对手 sample 总数、独立 FAIR SHA 数、模型预测耗时与缓存候选命中率。若同一个完整 `(owner, tick, FAIR SHA, checkpoint/controller SHA)` 重复出现，再考虑 root-local policy 分布缓存；缓存预测结果，不缓存随机动作，每次仍使用原 generator 采样，保证 RNG 流不变。

### 优化顺序

1. **当前版本无额外请求的内部计时。** 将 transition 互斥拆分为局部快照构造/释放、对手预测、native queue、batch/step 等待、接收与解码、rich 合并校验、tracker/tensorize、时钟及结束清理。同步记录每条边 tick 数、决策数、提前公共部署、动作终结原因及对手信息哈希重复率。只加宿主计时器，不增加 observe 或 status 请求；嵌套 RPC 单独作为子计数，避免重复加总。
2. **先测 event-batched JSON belief worker。** 将 native 请求从“每 tick 一次 step + 一次 observe-atomic”改为按下一个 WAIT/PLAY 截止 tick 请求，worker 在双方可见部署或终局时提前停止，并返回该边界的原子捕获。PLAY 截止仍须精确单 tick 执行和验证回执。它省的是无边界区间的宿主往返、捕获、解码和重复校验，不省 native tick。SSPI `wake_refills=False`，补牌仍只作为输入状态更新；已知 refill tick 会限制 batch 跨度以核对原生到位，但不新增决策。记录单客户端控制/batch 切换的重新握手耗时。
3. **再将 compact typed capture 与 event batching 组合。** batch channel 两种能力可同时启用，但当前 SSPI event 客户端假设 JSON mapping；需要适配 `TrainingAtomicCaptureV1`、保留 rich 校验，并在 restore 后将 `_last_tick` 和 binary/JSON event cursor 重定基。相邻 FAIR 搜索旧版本 compact 实测约 2.40×，只作可行性证据，不能推算当前 SSPI 提速倍率。
4. **单独去掉 transition 成功边的局部 snapshot。** 对照 0.496 秒 native RPC 基线及 Python 复制计时；不与 transport 变化同一轮实施，避免归因混淆。若批量客户端重连频繁，可以把该实验提前，先获得独立快照收益，再测其与批量路径的组合。
5. **基于重复帧率决定推理缓存。** 命中率高才实现，并验证每次原始 generator 采样后态和动作分布一致；若帧几乎都唯一，跳过缓存。

在同一冻结 root/model/seed 下串行比较当前路径、event-batched JSON、event-batched compact，分别至少完成完整 128 次有效模拟。记录 decision/deployment/refill 边界序列、每个 FAIR SHA、对手采样 RNG 后态、候选/访问分布、选定动作、root snapshot digest、handle 归零、tick 总量、batch 连接新建次数/耗时与服务端身份。边界、轨迹或访问统计不一致时不采用优化。现有普通 persistent `session-v1` 已避免每个普通请求重新建立 TCP 连接；batch 路径则需重新计量控制请求导致的通道重建，不用旧路径的连接假设推算倍率。

## 下一次受控原生实验

使用已确认空闲的专用测试 worker，不使用正在运行或状态未确认的端点。冻结同一个原生根、模型和随机流，串行比较当前 persistent 单 tick 路径与 resident event-batched 路径。每次严格完成 128 次有效模拟，不截短时域。报告根墙钟、belief ticks、native step/batch/capture 数、响应字节、推进与解码计时、decision/deployment 边界序列、访问分布与所选动作 SHA、根 FAIR SHA、恢复后状态摘要、snapshot handle 数和错误/重试。

只有所有语义身份与完整访问分布逐项一致，且恢复和补牌测试通过，才采用该路径。之后再针对新的 transition 剖析决定是否投入 compact capture、Python 快照复制或策略推理去重。

## 参考代码与证据

- 搜索阶段与 RPC 基线：[SSPI 搜索热点原生对照](SSPI_SEARCH_HOT_PATH_NATIVE_CONTROL_2026-11-02.json)、[静态编码与摘要优化结果](SSPI_SEARCH_HOT_PATH_OPTIMIZATION_2026-11-02.md)
- 当前根拆解及其版本限制：[SSPI 搜索耗时拆解](SSPI_SEARCH_BREAKDOWN_2026-11-02.md)
- 采集器的事件批量实际客户端与普通 belief 客户端：[collector.py](../firstlight_CR/native_runner/training/sspi/collector.py)
- 跨 tick 调度、预约及补牌边界：[native.py](../firstlight_CR/native_runner/training/fair_final/native.py)
- belief-world transition 与快照恢复：[search_runtime.py](../firstlight_CR/native_runner/training/fair_final/search_runtime.py)、[rollout.py](../firstlight_CR/native_runner/training/fair_final/rollout.py)
- 原生事件停止条件：[resident_multimatch.inc](../firstlight_CR/native_runner/probe/resident_multimatch.inc)

## 本轮发布副本验证

Python 3.13 下主引擎定向检查 `273 passed`；公开检查 `1,104 passed / 42 skipped`；公开清单校验 `2,829` 个文件通过。Gitleaks 扫描约 42.44 MB 无发现，`git diff --check` 通过。本次未运行新原生搜索或修改训练代码；验证结果是工程/发布检查，不是训练闭环或模型强度结论。
