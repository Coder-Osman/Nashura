# 事件调度统一修复与暂停记录

用户要求先停止正在运行的训练，再统一修复事件调度。

## 当前状态

最新进展：用户随后要求停止重评，并从 551 使用新调度追加 100 轮。旧评估已停止，保留 1,462 局；新 run 为 `event-residual-v3-scheduler-100r-20260919`，目标 651。第 552 轮完成并归档，553 已继续采集。详见 `evaluations/continuation-scheduler-100r-20260919/RUN.md`。以下为此前暂停与修复时的记录。

操作时训练已完成到第 551 轮，自动评估正在进行。已停止五台机器上的 pipeline、评估 worker 及相关进程；最终检查活动训练/评估进程为 0，108 个原生引擎端口均已释放 resident 会话并空闲，五台机器均保留 STOP 标记。

最终完整权重 SHA：
`a0c17d2308ad900d5d210b69109672d21755f827f152eae3634c0e21e1260613`

五台机器上的第 551 轮权重均独立通过 SHA 校验。修复和验证没有执行 PPO 更新。旧自动评估已完成的 594 局保留，并通过 scheduler-invalidated.json 标明受旧调度影响，不并入修复后的评估。

## 修改

- 新增共用 event_scheduling.complete_event_commands：单方出牌时，缺席方显式使用 1 tick 中性 WAIT。双人事件边界不再受 BattleEnv 默认 5 tick 决策等待影响。
- 采集 play_match、逐 tick 参考运行器、demo_event_selfplay 共用该规则。checkpoint round-robin 和常驻 worker 调用同一 play_match，因此评估和常驻采集同步修复。
- 显式提供的长等待与通用 BattleEnv 的独立截止时间语义保留；不通过强行清除所有 deadline 绕开环境契约。
- 演示入口同步使用 native_hand_slots 处理手牌空槽，统一其合法落点掩码和执行槽位。
- 新采集/演示统计标注 scheduler_version=event-scheduling.v2，便于与旧轨迹区分。

本地实现包含此前已有的法术约束；生产快照基于各自主机旧代码做精确补丁，没有将本地额外训练行为一起带入。

## 部署

五台机器 .34、.35、.37、.39、.41 均建立：

- collector-scheduler-code 与 collector-scheduler-manifest.json
- evaluation-scheduler-code 与 evaluation-scheduler-manifest.json

新快照以旧 collector-handslot-code / evaluation-handslot-code 为基础，文件采用原子替换；旧硬链接快照哈希保持一致。各 fleet.json 已指向新快照，训练和评估仍暂停。

主控 evaluate.py 与当前 pipeline 入口增加 STOP 检查，实际执行启动验证后均在开始工作前退出。下一次正式评估输出目录设置为 post-training-eval-scheduler-v2，旧 post-training-eval 留作证据。不要直接恢复旧评估 worker 或把旧 config.json 的结果混入新目录。

## 验证

- 本地相关测试 75 项通过；测试夹具兼容旧评估接口后新增 5 项再次通过。
- 把 rollout 临时恢复成旧的缺席方行为时，两项交错出牌回归测试分别在 owner 0 / owner 1 先出牌的情形失败；修复后通过。
- 远端实际采集快照 48 项测试通过；实际 CPU 评估快照 42 项通过。
- 第 537 轮、原种子 937091901：正式修复后双方各 42 次决策，出牌 22 / 23 次，tick 3601 正常终局，窗口跳过和窗口取消均为 0。45 条原生出牌记录与先前隔离实验的 owner/cardId/executeTick/坐标逐条一致。
- 第 551 轮经实际常驻 GPU 推理、合批服务及延后观测路径连续两局：同一 worker，每局双方各 61 次决策、60 次出牌，tick 4222 正常终局。动作、奖励、时间线完全一致；窗口跳过、窗口取消、非法动作均为 0。
- 原学习器读取这两局共 244 条完整行为数据，概率回算通过，最大绝对 log-ratio 分别为 2.861e-6 和 5.722e-6；重复采集的概率/熵浮点差异最大为 3.338e-6。
- 实际 CPU tournament 第 551 对 537 轮完成同种子换边两局，tick 3601 / 4097 终局，窗口跳过、窗口取消和非法动作均为 0。此处两局仅用于验证入口。
- 演示 CLI 第 551 轮自博弈完成终局，双方各出牌 22 次、各 45 次决策。演示日志的旧计划替换取消计数属于事件重新规划，与本次修复的窗口取消不同。

## 后续使用

训练保持暂停。用户随后已授权在修复后的评估流程重评第 551 轮及关键旧 checkpoint；正式评估已独立在后台启动。现有权重保留，不根据旧调度下的胜率直接判断最佳模型。

### 第 551 轮重新评估已启动

- 对阵：551 对 251、351、451、501；每组 540 对同种子换边，共 4,320 局。
- 五台机器 108 个 worker，统一使用 evaluation-scheduler-code；种子、对阵和评估参数预先固定，失败时保留原种子重试。
- 新目录：各机 run 下的 `post-training-eval-scheduler-v2`，与旧评估完全独立。
- `.34` 主控 PID：645716；日志：run 下的 `evaluation-scheduler-v2.log`。
- `.34` 入口增加显式 `--allow-paused-evaluation`；默认暂停保护已验证，所有训练 STOP 保留，未启动训练。
- 汇总时逐局核验 SHA、契约、种子、配对、真实终局及 `event-scheduling.v2`，要求窗口跳过/取消及非法动作均为零。
- 完成后主控自动汇总 `report.json`、`report.md`、`matches.json`、`status.json`；训练状态仍为第 551 轮完成，pipeline 状态另行注明 evaluation-only 和 training_paused。
- 最新启动检查保存在 `evaluations/scheduler-fix-20260919/evaluation-started-status.json`。
- 用户要求启动后停止跟进，等用户再次发起查询；本次没有创建定时监控。

复现脚本、测试日志、部署后的两套实际源码、manifest、五机停止/切换/最终状态，以及实战和概率验证报告均位于：
`evaluations/scheduler-fix-20260919/`

远端对应目录是 run 根目录下的 scheduler-fix-20260919。所有验证对局独立于原训练轮次与旧正式评估目录。
