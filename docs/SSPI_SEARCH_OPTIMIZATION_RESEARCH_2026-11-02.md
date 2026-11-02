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
