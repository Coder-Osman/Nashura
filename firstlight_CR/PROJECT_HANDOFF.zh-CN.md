# PUCT 树搜 RL 接管文档

更新时间：2026-10-01

本文只覆盖当前 `hog-tree-search-1755-200r-20260929` 树搜强化学习流程。通用 IL、旧版 PPO、Web 工作台和历史实验报告不属于本次接管主线；它们只作为 PUCT 运行时依赖存在。

## 1. 当前目标

从第 1755 轮 Hog checkpoint 继续训练 200 次更新：

```text
当前策略 + 原生离线引擎
        -> PUCT 根节点搜索
        -> 教师访问概率 / 冻结对手回应估值 / 终局证据
        -> 教师蒸馏损失

当前策略 + 冻结对手池
        -> 独立 on-policy PPO 对局
        -> PPO 损失

教师损失 + PPO 损失
        -> 同一轮 AdamW 更新
        -> 新 checkpoint
```

教师数据和独立 PPO 数据是两条不同的数据来源：教师局提供搜索监督，PPO 局提供行为策略分布和优势估计。教师局的动作不写入 PPO 行为数据。

## 2. 源码位置

归档后路径如下：

| 路径 | 作用 |
| --- | --- |
| `research/puct/tree_search.py` | PUCT 核心、搜索节点、状态版本和回应估值 |
| `research/puct/native_adapter.py` | 原生快照与 rollout sidecar 的一致性适配 |
| `research/puct/event_policy_rollout.py` | 事件决策、合法动作、策略先验、价值和轻量状态恢复 |
| `research/puct/distillation.py` | 教师记录 v2/v3、输入校验、episode 切分、教师损失 |
| `research/puct/teacher_targets.py` | Top-4 回应目标和终局证据 |
| `research/puct/teacher_training.py` | 教师 objective 与 batch 统计 |
| `research/puct/train_with_teacher.py` | formal/integration 更新入口和阻断条件 |
| `research/puct/collect_policy_episode.py` | 独立 PPO 对局采集 |
| `research/puct/formal_contract.py` | 五机配额、216 教师局、108 PPO 局的固定合同 |
| `research/puct/formal_batch.py` | 远端阶段启动、状态查询、对手准备 |
| `research/puct/formal_host_worker.py` | 单机并发槽位和任务生命周期 |
| `research/puct/fleet_preflight.py` | 引擎、资源、代码/权重身份预检 |
| `native_runner/contracts.py` | `ObservationV1`、`ActionV1`、`EpisodeConfigV1` 等基础接口 |
| `native_runner/battle_env.py` | `BattleEnvV1`，PUCT 所需原生环境和 FAIR 观测 |
| `native_runner/training/event_*.py` | 事件模型、动作空间、奖励、rollout 和 PPO 依赖 |

`research/puct/` 来自当前 PUCT 实验源码快照；归档不包含该目录中的运行 JSON、日志、引擎、权重和 AVD。

## 3. 一轮训练的生命周期

### 3.1 预检

`fleet_preflight.py` 逐台检查：

- 任务配置、代码入口和父 checkpoint SHA。
- supervisor/native child 的 PID、启动时间、进程归属和停止标记。
- engine 的 `ok`、`coldReady`、`contentReady`。
- 端口、内存、磁盘和 resident build/shared library。
- 五机固定端口配额和 host 资源。

只有整体报告通过，正式阶段才进入采集。机器清单和认证材料来自外部部署环境，源码包中只保留调用接口。

### 3.2 教师采集

`formal_batch.py launch --phase teacher --cycle C` 分发 216 局教师任务。每局默认 128 次模拟，高不确定性可使用 256；原型验收使用 32 次。

单局流程：

1. `EventPolicyRollout.reset()` 初始化双方同卡组对局和随机种子。
2. 到达行动窗口后，`capture_search()` 保存根状态。
3. `PuctSearch.run()` 每次模拟从根状态恢复，选择根动作/对手动作，调用 adapter 推进到下一个决策点或搜索时域。
4. 对根动作调用 `evaluate_root_replies()`，按冻结对手先验取 Top-4 回应，计算 `Q_expected`、`Q_robust` 和 `Q_target`。
5. 选访问次数最高的根动作，恢复真实根状态后只执行该动作。
6. 继续按冻结策略推进到终局，写入完整终局证据。
7. 只有状态摘要、动作执行账本、回应估值和终局均完整时，才落盘 v3 教师记录。

教师局在每个可行动根节点搜索，不是只在整局根节点搜索。每个根节点的模拟深度上限是 6 个决策点，墙钟推进预算为 10 秒；`128/256` 是重复模拟次数，不等于搜索深度。

### 3.3 独立 PPO 采集

教师阶段全部通过后，`formal_batch.py launch --phase ppo --update U` 分发 108 局独立 PPO 数据：

| 类型 | 局数 | 作用 |
| --- | ---: | --- |
| `self_play` | 72 | 当前策略自博弈，产生 on-policy 行为轨迹 |
| `fixed_1250` | 12 | 固定历史对手，检查策略退化和稳定性 |
| `recent` | 24 | 近期冻结对手，保持训练分布连续 |

这些对局关闭树搜，使用当前策略采样动作。记录行为 log-prob、value、reward、decision tick、终局和 opponent identity，供 PPO preflight、GAE 和更新使用。

### 3.4 更新

`train_with_teacher.py --mode formal` 在一次 AdamW 更新前完成：

1. 教师记录 v3 完整性、输入契约、掩码和回应证据校验。
2. PPO rollout lane 完整性、行为概率重算和 checkpoint identity 校验。
3. 依据 update index 计算教师 policy/value 权重；当前前半程为 `0.50/0.10`，后半程线性衰减到 `0.25/0.05`。
4. 教师策略损失使用访问概率非零动作在完整动作空间上的联合 log-prob；不在访问子集内重新归一化。
5. 教师价值项使用 `Q_target` 的 Huber 损失。
6. 教师项与 PPO 项一起反向传播、梯度裁剪和 AdamW step。
7. 检查有限参数、参数发生变化、optimizer step 连续、checkpoint 可读和 SHA。

`--mode integration` 只写入本实验的 `training-check/` 证据；正式入口必须满足五机预检、原型验收、216 教师记录和 108 PPO 数据门槛。

## 4. 核心状态接口

### 4.1 `SearchState`：`tree-search-state.v1`

```python
SearchState(
    native_snapshot,  # 原生完整分支状态
    observation,      # 未截断的 fair/native 观测
    actor,            # 0 或 1
    legal_actions,    # 去重后的合法动作 key
    external_queue,   # 外部动作/到期计划队列
    reward_ledger,    # 当前奖励和执行账本
    event_cursor,     # seen/combat/public-card 等事件游标
    policy_state,     # rollout/tensorizer/recurrent 侧状态
    terminal,
)
```

`to_dict()` 编码带 `version`；`from_dict()` 要求字段集合与版本完全匹配。非终局状态必须有合法动作。搜索恢复前必须验证 native fingerprint 和 sidecar 一致性。

### 4.2 `NativeTreeSearchAdapter`

实现必须提供：

```python
save_search_state() -> SearchState
restore_search_state(state: SearchState) -> None
apply_action(action: str) -> None
advance_until_decision(max_decision_points: int, max_native_seconds: float) -> Transition
fingerprint() -> str
```

`NativeSnapshotSearchAdapter` 负责：

- 原生完整快照句柄的获取、恢复和释放。
- `BattleEnvSidecar` 中事件游标、奖励 ledger、pending command、tracker 和策略状态的同步。
- 恢复后 native digest、sidecar digest、合法动作和行动方的校验。
- 快速内部 capture/restore；搜索热路径不经过 JSON 编解码。

### 4.3 PUCT 节点和目标

`SearchNode` 为每个合法动作维护 `prior`、`visits`、`value_sum` 和子节点。根节点按 PUCT 选择：

```text
Q(action) + cpuct * P(action) * sqrt(total_visits) / (1 + visits(action))
```

当前合同：`cpuct=1.5`、对手 Top-4、对手温度 `0.5`、`gamma=0.999`、训练期根节点 Dirichlet `alpha=0.25/epsilon=0.25`。搜索缓存使用 `state_digest`/快速 `state_key`；`compact_children=True` 时只保留必要 child 状态。

冻结对手回应必须全量估值：

```text
Q_expected = sum(probability_i * value_i) / sum(probability_i)
Q_robust   = min(value_i where probability_i > 0)
Q_target   = 0.7 * Q_expected + 0.3 * Q_robust
```

缺少 Top-4 中任一正概率回应时，教师目标保持无效，不写入训练记录。

## 5. 教师记录接口

`TeacherRecord` 版本：

- `hog-tree-teacher-record.v2`：根节点 `semantic_frame`、`gate_mask`、`card_mask`、`cell_masks`、`delay_mask`、访问概率和搜索统计。
- `hog-tree-teacher-record.v3`：v2 加 `value_evidence`、`terminal_evidence`、状态摘要和完整终局证据。

主要方法：

```python
TeacherRecord.from_dict(value)
record.validate()
record.require_training_ready()
record.model_inputs(device)
split_episodes(records, seed=1755)
scheduled_weights(update, total_updates=200, ...)
distillation_loss(policy_log_probs, teacher_probs, value, target, ...)
```

v3 进入更新前必须通过：整局未截断、根动作可执行、Top-4 回应集合完整、终局胜负与 native 证据一致、模型输入和 mask 维度匹配、checkpoint/contract identity 匹配。

## 6. 远端任务接口

### 6.1 五机合同

正式教师/采集配额由 `formal_contract.py` 生成：

```text
HOST_ORDER = (HOST_A, HOST_B, HOST_C, HOST_D, HOST_E)
engine slots = 24 / 24 / 24 / 18 / 18
teacher games per cycle = 216
independent PPO games per update = 108
```

实际 host/IP、用户名、SSH 密码和私钥从外部 inventory 注入；归档中的 `HOST_A` 等仅代表角色。

### 6.2 调度命令

```bash
python formal_batch.py prepare-opponents --output formal/opponent-deployment.json
python formal_batch.py launch --phase teacher --cycle 1
python formal_batch.py status --phase teacher --cycle 1 --output formal/teacher-status.json
python formal_batch.py launch --phase ppo --update 1
python formal_batch.py status --phase ppo --update 1 --output formal/ppo-status.json
```

这些命令只负责远端采集、状态和汇总，不执行本机采集，也不执行 optimizer update。更新由训练执行主机运行 `train_with_teacher.py`。

### 6.3 生命周期规则

- 以 cycle/update 目录作为幂等边界；完成目录不重复采集。
- 每个任务有独立 `episode_id`、seed、端口、输出、日志和 pid 记录。
- host worker 按 `CONCURRENCY` 计算空闲槽位；已有归属且健康的 engine 复用。
- 失败任务写入失败状态并保留证据；修复后只重试未完成任务。
- 教师和 PPO 两个阶段不能同时抢占同一端口或同一 engine。

## 7. 性能接管重点

当前搜索慢点应按层计时：

1. native `step`/推进到下一个决策点。
2. native capture/restore 和 handle 管理。
3. `BattleEnvSidecar` 的事件、奖励、pending 和 tracker 恢复。
4. FAIR 观测、语义张量和合法动作 mask 构造。
5. policy prior/value 推理。
6. PUCT Python 节点选择、状态摘要和 child 管理。
7. worker IPC、日志和结果落盘。

已落地的快速路径是内部 `capture_search`/`restore_search`、可选 `copy_capture`/`validate_state`、快速 `state_key`、紧凑 child 状态和同机微批推理。优化时必须保留恢复后 fingerprint、事件 cursor、执行账本和终局一致性测试。

不要把“每节点模拟 8 次、每次 6 步”直接乘成总耗时：实际路径可能提前终局或在决策窗口结束，且每个模拟包含完整状态恢复、原生推进、观测/事件处理和模型推理；`max_native_seconds` 只约束 native 推进墙钟，不覆盖整个搜索调用。

## 8. 验证清单

纯 Python 检查：

```bash
python -m compileall research/puct native_runner
python -m pytest research/puct/test_tree_search.py research/puct/test_teacher_targets.py
python -m pytest research/puct/test_event_policy_rollout.py research/puct/test_teacher_training.py
```

PUCT 接管前必须看到：

- tree search 状态 round-trip 和非法字段拒绝。
- native/sidecar 恢复后 fingerprint 与事件游标一致。
- 缺失 Top-4 回应、错误终局、截断记录全部被拒绝。
- 教师损失只消费教师记录，PPO loss 只消费独立 PPO lane。
- PPO preflight 通过，行为概率重算误差在阈值内。
- optimizer state、参数顺序、父 checkpoint SHA 和新 checkpoint SHA 连续。

真实原生验收和正式采集必须在远端执行；源码包只支持静态检查、单元测试和接口审计。

## 9. 不随包交接的内容

明确排除：checkpoint/权重、教师 JSONL、PPO episode、replay/parquet/cache、PUCT 评测结果、日志、PID、AVD/guest、APK/so/native 镜像、录屏、真实 `.env`、机器清单、SSH 密钥和密码。它们的路径、SHA 和生命周期由远端运行目录或秘密管理系统单独交接。
