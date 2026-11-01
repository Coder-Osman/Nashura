# SSPI 1024 根搜索：已落地的低风险优化与 Codex 实机任务

> 代码基线：`main@339dd64b8514fe19418042c503d9ecc2035d8648`。本分支只修改不依赖硬件的计算路径与性能观测，不改变 MCTS 深度、预算、控制时钟、FAIR 可见性、native 传输后端或模型参数。下面的速度收益**未经 M4 Max/Android AVD 实测**。

## 新增和改变的代码

1. `sspi/model.py`：`SSPIPolicy.conditionals` 增加**每次策略前向实例内部的前缀缓存**，避免同一冻结状态反复计算 mode/card/cell/delay log-softmax；保留 autograd 图，更新网络后不得复用旧策略对象。
2. `sspi/search.py`：将 `SSPIJointPolicy.topk_conditionals` 从576次 Python 浮点提取改为一次稳定 `torch.argsort`，排序规则仍是得分降序、同分按格点/延迟索引升序。
3. `fair_final/search.py`：增加 `candidate_generation`、`tree_selection` 顶层计时，均直接包裹原调用，不改变参数和返回值。旧有 `transition` 等项目仍保留。
4. `sspi/search_probe.py`：修正错误的 `native_ticks=timings` 字段；独立信念世界的每次成功 rollout.advance 按控制器 tick 前后差累计**实际推进tick**；记录 native RPC 命令数/耗时以及搜索节点推理和对手采样耗时。每种探针都只观察已有调用，没有增加状态查询或修改随机流。
5. `sspi/performance.py`：小型、纯本地计数器，区分**顶层互不重叠阶段**与嵌套的 RPC/对手推理。请求层的 `successful_step_ticks_requested` 只是成功 step 命令请求的 tick 总和，不等于实际推进 tick，后者见 `native_ticks` 和 `performance.native_ticks.actual_ticks`。
6. `tests/test_sspi_search_performance.py`：同分排序、非法位置、缓存梯度、native 请求计数、真正 tick 差、对手随机流与搜索计时的定向回归。

不变：搜索预算 **1024/128**、深度/时域、Hog-only、五档200–1000ms、WAIT20ticks、仅搜索根训练、独立 FAIR belief、原生战斗结算。没有切换新 native 后端、连接协议或改变离散随机数用法。

## 现有实测和数据解释

2026-11-01 历史 P0/P1 完整根：1024/1024，712.48秒。原有分项：转移495.44秒，网络推理94.34秒，根恢复16.62秒，信念构造1.92秒；未单独归类约104.16秒。这是**旧代码基准**，不是本分支优化后结果。

- 顶层 `audit.timings` 的6项（belief/restore/inference/transition/candidate_generation/tree_selection）原则上可求和；其他辅助计时只作为其中的**嵌套诊断**，绝不能直接相加。
- `opponent_sample_seconds` 在 `transition` 之内，不等于原生 step。
- `belief_native_rpc` 是独立信念端点从建立世界到搜索结束的RPC观测；`actual_root_native_rpc` 是真实根侧在搜索调用期间的RPC观测（正常应极少）。
- `native_ticks` 是独立信念 rollout 成功提交后的实际时钟增量；根快照恢复不计入该值。
- 实测探针默认采用 `LegacySearchNative`，没有持久 TCP 和 compact/resident；**本分支没有私自启用**。

## Codex 必须执行的本机任务（按次序）

1. 在 Mac 的 Android ARM64 AVD 环境检查当前 main/本分支代码；不要调用 VMware Linux guest。运行新定向测试与原有 SSPI/FAIR 搜索/快照测试、语法检查、公开检查及 gitleaks，修复兼容问题后才合并到正式主线。
2. 用**同一个冻结开局根、同样种子、同样初始权重与合法性**对比原 `339dd64b` 和本分支：候选 action ID/排序/先验、1024 visits、所选动作、state digest、FAIR SHA、对手采样随机序列及原生回执需保持一致。优化后的 `search_protocol_sha` 不得因仅性能改动而变化。
3. 先用新探针 `--simulations 128` 跑兼容诊断，统计实际 native ticks、按 RPC 类型的调用次数和时间、候选生成、树选择、leaf infer、对手 sample、转移剩余时间，检查各项无负值且总量口径一致。**128次诊断不能伪装成完整1024训练根**；通过后再用1024做同根受控回归。主实验预算始终只允许128/1024。
4. 若观测和连接占主要耗时，**单独**测试 legacy engine 的 `session-v1` 持久 TCP 支持；若支持，验证跨 snapshot-create/restore/observe-atomic 的回执和完整张量等价，且确保外部 actor 不被打断。未验证前不要默认启用。
5. 调查能否把旧 `fair_search/transport.py` 的 compact/resident 方案用于当前 Android AVD 引擎。它具有不同的 resident/slot 协议，不能简单替换类名；需先验证 capabilities、引擎端口以及全部 FAIR 观测事件一致性。特别留意控制操作可能关闭并重开 batch channel。
6. 如果仍由 native tick 内的事件解码和历史窗口重复校验主导，再考虑当前引擎的 delta/cursor 增量捕获；不要无证据跳过 phase/combat/visibility 事件。历史 `step-until-event` 已发现有观察张量不等价风险。
7. 优化前后逐状态重放真实序列，至少比较完整根1024访问分布与所选动作，性能报告列出速度提升、实际搜索吞吐（valid_simulations/wall_seconds）、实际 tick 速度、保留的所有正确性门以及剩余未归类时间。
8. 再继续 P2 完整真实混合对局、P3 搜索根损失与 P4 关闭搜索评测。不要擅自增加新模型、多 seed、大规模并发或时间上限。

## 可能的后续瓶颈（尚未实施）

- 由于 `LegacySearchNative` 当前每个 tick 走 `step 1` 与 `observe-atomic`，还可能每条请求分别建连，连续战斗速度不能直接代表MCTS速度。
- `FinalSnapshot.restore` 与 `RolloutSnapshot.restore` 会调用 native restore、观测、sidecar rebasing、FAIR张量复制；目前恢复16.62秒并非首要瓶颈。
- 推理单次耗时还取决于 actor/leaf 调用数和 PyTorch CPU 小算子；`CPU/MPS` 快慢须测真实新负载。
- 多 tick 跳跃可能错过部署、补牌到位和同 tick 决策边界。只有保证逐状态可见事件与回执等价，才能考虑跨 tick 快进。
