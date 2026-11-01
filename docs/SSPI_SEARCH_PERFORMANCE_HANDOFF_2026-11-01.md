# SSPI 1024 根搜索：M4 Max AVD 实测与低风险优化

> 代码基线：`main@339dd64b8514fe19418042c503d9ecc2035d8648`。实测机器为 M4 Max macOS + `astral-api30` ARM64 Android Emulator，engine SHA `110aa2b5…959783`、probe SHA `f5a69101…7f346f`。搜索模型为 CPU 上随机初始化的7,874,126参数模型，seed `20261101`，model SHA `49c956b1…4993b3`，root FAIR SHA `30050324…e674`。1024 baseline、PR legacy、PR persistent 的搜索协议 SHA、模型 SHA、belief sampler SHA和完整根访问分布相同。

## 新增和改变的代码

1. `sspi/model.py`：`SSPIPolicy.conditionals` 增加**每次策略前向实例内部的前缀缓存**，避免同一冻结状态反复计算 mode/card/cell/delay log-softmax；保留 autograd 图，更新网络后不得复用旧策略对象。
2. `sspi/search.py`：将 `SSPIJointPolicy.topk_conditionals` 从576次 Python 浮点提取改为一次稳定 `torch.argsort`，排序规则仍是得分降序、同分按格点/延迟索引升序。
3. `fair_final/search.py`：增加 `candidate_generation`、`tree_selection` 顶层计时，均直接包裹原调用，不改变参数和返回值。旧有 `transition` 等项目仍保留。
4. `sspi/search_probe.py`：修正错误的 `native_ticks=timings` 字段；独立信念世界的每次成功 rollout.advance 按控制器 tick 前后差累计**实际推进tick**；记录 native RPC 命令数/耗时以及搜索节点推理和对手采样耗时。每种探针都只观察已有调用，没有增加状态查询或修改随机流。
5. `sspi/performance.py`：小型、纯本地计数器，区分**顶层互不重叠阶段**与嵌套的 RPC/对手推理。请求层的 `successful_step_ticks_requested` 只是成功 step 命令请求的 tick 总和，不等于实际推进 tick，后者见 `native_ticks` 和 `performance.native_ticks.actual_ticks`。
6. `sspi/search_probe.py`：保留默认 `legacy` transport，增加显式 `--transport persistent` 选项，用已存在的 `session-v1` 控制连接运行诊断；所有实际根与 belief 客户端在 finally 中关闭持久 socket。
7. `tests/test_sspi_search_performance.py`：同分排序、非法位置、缓存梯度、native 请求计数、真正 tick 差、对手随机流、搜索计时和transport参数校验的定向回归。

不变：搜索预算 **1024/128**、深度/时域、Hog-only、五档200–1000ms、WAIT20ticks、仅搜索根训练、独立 FAIR belief、原生战斗结算。没有切换新 native 后端、连接协议或改变离散随机数用法。

## M4 Max / AVD 实测

AVD `emulator-5554` 的 root/belief 端口为26789/26790，三种配置使用 seed `20261101`、同一CPU模型初始化和固定1024模拟预算。每一轮均完成1024/1024有效模拟、0条丢弃边、实际推进31,224个belief native ticks、根 tick=90，且本轮搜索前后 native root digest相同，belief快照租约释放。`model_sha`、`controller_sha`、`search_sha`、`belief_sampler_sha`、root FAIR SHA、selected action及完整候选 action/prior/visits/Q分布逐项相同。

| 配置 | 根墙钟 | 有效模拟/秒 | 实际native ticks/秒 | 候选生成 | 转移 | 顶层未归类 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| base `339dd64b`, legacy | 486.01 s | 2.107 | 64.25 | 11.77 s | 340.27 s | 55.55 s |
| PR optimizations, legacy | 470.88 s | 2.175 | 66.31 | 3.65 s | 333.91 s | 55.70 s |
| PR optimizations, persistent `session-v1` | 403.81 s | 2.536 | 77.32 | 3.66 s | 268.17 s | 55.83 s |

候选生成缓存/排序使该顶层计时由11.77秒降至3.65秒（约69%）；整体legacy根比base快3.1%。在已验证 `session-v1` 上，persistent相对PR legacy根墙钟减少14.2%，吞吐提升16.6%。相对base legacy，persistent根墙钟减少16.9%，吞吐提升20.4%。只有一组受控搜索对照，不代表多状态平均速度。

1024 persistent根的RPC调用数和总耗时：

| RPC命令 | 次数 | 累计耗时 |
| --- | ---: | ---: |
| `step` | 31,225 | 21.23 s |
| `observe-atomic` | 32,251 | 30.41 s |
| `observe` | 2,403 | 2.03 s |
| `snapshot-create` | 2,176 | 1.72 s |
| `release-snapshot` | 2,176 | 1.65 s |
| `restore` | 1,026 | 0.98 s |
| other | 1,377 | 1.11 s |
| 合计 | 72,634 | 59.14 s |

legacy PR在相同命令次数下总RPC耗时128.63秒（其中`observe-atomic` 61.75秒、`step` 50.37秒）。persistent根的belief rollout累计31,224次advance，advance包围计时90.83秒；leaf/policy推理65.36秒（1,021次），对手策略采样142.39秒（2,175次）。对手采样与native RPC均为嵌套诊断，不能再加到顶层阶段。transition总268.17秒；扣除不重叠的opponent sample与`rollout.advance`包围计时后，约34.95秒属于transition其余Python/快照路径。顶层未归类55.83秒。以上口径不把nested RPC重复相加。

## 正确性与transport

- 128诊断先完成base/PR legacy/persistent的访问分布比对；base、PR legacy、PR persistent均为128/128，2,924实际ticks、同一selected WAIT、FAIR SHA和search protocol SHA，候选列表逐项完全相同。持久模式清理修复后再次运行PR persistent，结果仍一致。
- 1024 base legacy、PR legacy、PR persistent三份完整候选列表逐项相同，包含action ID及顺序、proposal、原始联合logp、projected/search/card prior、visits和Q值；实际tick数与搜索随机种子不变。每轮内的root摘要前后相同。
- `session-v1` 在同一worker接受 `snapshot-create`、step、restore、`observe-atomic` 和 snapshot release，并在多轮请求中复用同一socket。完整capture仅有restore语义导致的`stateEpoch`与snapshot句柄计数变化；剔除这两类元数据后完整ordinary/rich capture一致，两个snapshot均释放。persistent 1024整根本身也通过快照恢复和lease释放门。
- 不同独立reset的process-local native root digest并不相同；因此报告称为**相同FAIR冻结决策状态**（SHA相同）上的搜索对照，不声称原始引擎内部快照字节相同。每次运行内部仍检查native root digest不变。旧worker端口26791的一次初始化曾返回截断observe JSON、未进入搜索，该次不纳入性能数据。
- 搜索预算、搜索时域、游戏逻辑、FAIR可见性、网络参数、随机采样和训练损失未变。persistent作为probe显式选项，默认仍为legacy；本轮没有启用compact/resident或event-delta捕获。

历史首个工程根仍为1024/1024、712.48秒；它使用早期探针与不同运行时间，保留为历史记录，不纳入以上同轮对照。

- 顶层 `audit.timings` 的6项（belief/restore/inference/transition/candidate_generation/tree_selection）原则上可求和；其他辅助计时只作为其中的**嵌套诊断**，绝不能直接相加。
- `opponent_sample_seconds` 在 `transition` 之内，不等于原生 step。
- `belief_native_rpc` 是独立信念端点从建立世界到搜索结束的RPC观测；`actual_root_native_rpc` 是真实根侧在搜索调用期间的RPC观测（正常应极少）。
- `native_ticks` 是独立信念 rollout 成功提交后的实际时钟增量；根快照恢复不计入该值。

## 静态验证结果

在PR更新中运行：性能定向测试9 passed；SSPI/fair_final搜索、rollout、snapshot恢复与持久化回归276 passed，另加缺失本机include后phase snapshot 1 passed；公开检查1,104 passed/45 skipped；主引擎检查212 passed；语法检查、diff空白检查和gitleaks通过（约35.2 MB，无泄漏）。第一次隔离回归中，phase snapshot因公开快照未含外部C++ include失败；临时链接本机原附件后通过，未把该附件加入公开仓库。

## 后续工作

- persistent transport只落在性能探针的显式模式；要在训练collector采用前先把同一per-instance配置接入实际P2闭环，并运行真实完整局回归。
- 最大剩余成本是对手策略采样142.39秒、leaf推理65.36秒、tick advance/捕获90.83秒和约34.95秒transition其他路径。先用进一步分项探针分析Python事件/张量处理，不跳过FAIR事件，也不批量推进tick。
- 剩余未归类顶层时间约55.8秒，继续定位时须避免与嵌套RPC、对手推理和advance计时重复累计。
- P2真实混合完整对局、P3仅搜索根策略CE/WDL CE训练与checkpoint恢复、P4关闭MCTS独立对战评估仍未完成。完成性能PR前先保留这些未完成状态；其后继续5%×1024与唯一对照40%×128。
