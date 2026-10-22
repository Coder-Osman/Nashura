# 2.6 速猪树搜专精训练

这是从第 1755 轮继续的独立实验目录。学习方固定为 `hog`，正式第 1553 轮权重只作为回归基线，不参与参数更新。

本目录先提供可复现的树搜适配层、教师目标生成和训练计划校验。原生引擎通过 `NativeTreeSearchAdapter` 注入完整快照、事件游标、奖励账本和外部动作队列；搜索模块不会自行猜测缺失字段。真实五机采集必须在 `prototype_acceptance.json` 通过后运行。

## 固定合同

- 父权重：第 1755 轮，SHA `e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0`
- 学习卡组：`hog`
- 每轮：72 自博弈、12 固定 1250、24 近期对手
- 五机引擎：`.34/.35/.37/.39/.41 = 24/24/24/18/18`
- PUCT：原型 32、正式 128、高不确定性复查 256 次模拟
- 状态模式：`deterministic_full_state`；对手 Top-4，温度 0.5
- `Q_target = 0.7 * Q_expected + 0.3 * Q_robust`
- 训练期我方根节点 Dirichlet：`alpha=0.25, epsilon=0.25`
- 蒸馏系数：前半程 `0.50/0.10`，后半程线性衰减到 `0.25/0.05`
- 训练 200 次更新；最终只对第 200 轮和第 1755 轮进行 6000 局评估，树搜关闭

## 状态恢复

搜索状态版本固定为 `tree-search-state.v1`。节点必须同时保存原生快照、未截断观察、合法动作、外部队列、奖励账本、事件游标、循环策略状态和 `N/W/Q/P`。原生完整快照是唯一的分支状态来源；适配器在观察被截断、快照摘要不一致或上述字段任一缺失时拒绝恢复。卡组身份只用于绑定、对手配置和审计，不作为模型输入。

```bash
python3 control.py check
python3 validate.py
```

`control.py run-prototype` 会先检查任务配置和自有原生引擎，再执行真实原生 A/B/A 验收。现有 `native-acceptance.json` 只覆盖原生和 BattleEnv A/B/A、嵌套恢复与句柄释放，不代表 32 次模拟的 108 局原型已通过。没有真实策略、合法动作掩码和原生推进回执时，控制器保持停止，不启动五机教师采集。

原生验收必须核对完整快照摘要、公开事件游标、合法动作掩码和奖励账本；学习方策略输入仍只来自规定的观察结构，不注入卡组身份。计算教师价值时须对冻结对手策略的全部 Top-4 回应估值，使用 `frozen_opponent_target` 汇总；缺少任一回应时拒绝写入教师记录。

`PuctSearch.evaluate_root_replies` 对根动作逐一恢复确定性原生分支，再展开冻结对手的全部 Top-4 回应。它按冻结对手策略计算期望与保守值；到不了对手决策点或超过搜索时域的根动作不生成回应教师目标。

## 教师记录的模型输入

带模型输入的记录使用 `hog-tree-teacher-record.v2`：`semantic_frame` 保存搜索开始前根节点实际使用的 `UniversalSemanticBatchV4`，`action_masks` 保存同一决策的 `gate_mask`、`card_mask`、`cell_masks`、`delay_mask`。采集时保持浮点精度，输入来自 FAIR 语义张量，不从搜索后的观察反推。`TeacherRecord.from_dict` 读取记录，`record.model_inputs(device)` 恢复模型输入和布尔掩码。

旧 `v1` 记录仍支持读取、审计和原样导出；由于未保存语义帧，调用 `model_inputs` 会报错，需重新采集。`v2` 的输入字段、单决策批次、掩码类型与维度均有检查。当前 event 模型使用 stateless snapshot core，每个决策重置循环状态。

完成回应估值与整局结算的记录使用 `hog-tree-teacher-record.v3`，新增 `value_evidence` 和 `terminal_evidence`。前者包含冻结策略 Top-4 回应的完整概率、各分支估值和状态摘要；后者包含原生终局、胜方、双方结果、终局 tick 与状态摘要。`require_training_ready()` 重算期望/保守值，核对胜负标签，并拒绝旧版本占位目标、缺失回应、未结算或截断对局。

`event_puct_acceptance.py` 在同一个根节点完成 32 次搜索与全部 Top-4 回应估值，恢复并释放搜索快照后，执行选中的根动作，再按冻结策略采样到实际终局。只有成功结算才写出 `v3` 教师记录；该单局证据仍不替代 108 局原型验收。

## PPO 与教师蒸馏接入

`collect_policy_episode.py` 生成独立的完整 on-policy PPO 对局；教师搜索对局仅提供蒸馏目标，不混入 PPO 行为分布。`teacher_training.py` 对访问概率非零的动作计算原始完整动作空间的联合 log-prob，不在已访问子集内重新归一化。教师 CE 与 Q-target Huber 损失按既有系数累积，在同一个 PPO AdamW step 前参与梯度裁剪与更新。

`train_with_teacher.py` 先校验输入来源、完整对局、掩码与行为概率，再恢复第 1755 轮 AdamW 的参数顺序、学习率和动量。完成一次更新后检查有限参数、参数变化、优化器 step 连续性，并读回 checkpoint。`--mode integration` 仅允许写入本目录 `training-check/`，不推进正式实验。`--mode formal` 校验五机资源/引擎、108 局原型、每周期 216 局教师记录、每轮 108 局独立 PPO 数据及 `72/12/24` 对手配额；教师数据按整局划分，只有 train 部分参与更新。

```bash
# 在本机读取五机状态，沿用已有本地登录清单，不修改主机配置。
python3 fleet_preflight.py
# 将新鲜的 fleet-preflight.json 同步到执行主机后，尝试正式首轮更新。
python3 control.py start-formal --teacher formal/teachers/cycle-001/teachers.jsonl
```

正式入口按 `--update-index` 约束 1–200 次更新，每次必须输入与父 checkpoint 匹配的新 PPO 数据；它不会用验收样本冒充正式采集规模。当前自动化五机教师采集和后续轮次调度仍待部署接通。启动缺口写入 `formal-start-attempt.json`，单次集成更新证据写入 `training-check/result.json`。

## 五机部署与预检

`fleet_deploy.py HOST` 从 `.37` 复制已验证的 Linux 运行包，排除引擎实例和采集输出，修复目标主机的资源链接，并安装本机固定的任务配置。关键文件 SHA-256 校验包括父 checkpoint、QEMU、resident supervisor、任务入口及配置。任务配置通过临时文件和原子重命名发布，避免读取半写入 JSON。

```bash
python3 fleet_deploy.py 34
python3 fleet_engines.py 34 --workers 4
python3 fleet_preflight.py
```

启动并发限制为 1–4，默认串行；每个端口仍由 `server_engine.py` 检查停止标记、进程归属、端口占用、32 GiB 内存及 20 GiB 磁盘余量。重复调用复用已有就绪进程，不启动正式训练。

只读预检逐一查询固定配额内的原生引擎 `status`，检查 supervisor 与 native child 的 PID/启动时间、`ok/coldReady/contentReady`、五机资源和停止标记；同时核对任务配置、入口、resident build 及共享库、1755 父权重的 SHA-256。失败引擎单独记录，不计入就绪数量。结果写入 `fleet-preflight.json`，包含 `passed`、`blockers`、逐端口状态和运行文件哈希；正式训练要求报告在 300 秒内且整体通过，旧的仅计数报告需重新生成。

## 正式数据采集

`prototype_batch.py status` 已确认五机原型 `108/108` 完整终局、32 次模拟通过。开始正式采集前重新运行只读 `fleet_preflight.py`，核对 108 个任务引擎、快照句柄、磁盘和运行时哈希。正式数据由远端 worker 产生，本机只负责 SSH 调度与汇总状态。

```bash
python3 formal_batch.py launch --phase teacher --cycle 1
python3 formal_batch.py status --phase teacher --cycle 1 --output formal/teacher-status.json
```

第一周期教师固定为 216 局、每局 128 次模拟，按五机端口配额循环两次；结果保存在各主机的 `formal/teachers/cycle-001/records/`。只有全部记录通过身份、终局、执行账本和快照句柄校验后，才开始独立 PPO 对局采集：

```bash
python3 formal_batch.py prepare-opponents --output formal/opponent-deployment.json
python3 formal_batch.py launch --phase ppo --update 1
python3 formal_batch.py status --phase ppo --update 1 --output formal/ppo-status.json
```

PPO 第 1 轮固定 108 局，按 `72` 自博弈、`12` 固定 1250、`24` 近期对手分配；不使用树搜动作。PPO launch 会强制检查教师 216/216 已通过，并复核五机冻结对手权重 SHA，避免两个队列同时占用同一端口。`formal_batch.py` 只启动远端采集进程，不执行优化器更新；正式训练还需要把各机完整数据汇集到执行主机，并通过 `train_with_teacher.py` 的新鲜预检与输入校验。

## 单次模拟深度

当前配置为最多 6 个决策点，每个决策点可来自任一方。搜索从根节点沿树选择动作，首次遇到未展开子节点便调用价值网络并回传；也会在终局或达到预算时结束。因此 6 是单条路径的上限，32/128/256 是重复模拟的次数，实际深度不一定到 6。

`max_native_seconds=10` 在当前 event 适配器中使用 `time.monotonic()` 累计原生推进调用的墙钟耗时，并非游戏内 10 秒，也不包含整次搜索的全部推理、摘要和快照操作。单次推进超过剩余预算会使验收报错。当前验收未保存逐次模拟深度，实际均值需增加统计后另行测量。
