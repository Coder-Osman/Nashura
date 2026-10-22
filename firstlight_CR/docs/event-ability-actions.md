# 事件策略的主动技能动作

2026-09-25：在事件策略中加入可选的技能动作分支。当前 1553 / 新旧
1753 仍是出牌策略权重；接口升级本身不代表这些权重已经学会技能。

## 动作与事件

启用 `EventModelConfigV1(ability_actions=True)` 后，每次决策选择：

| 分支 | 参数 | 执行时间 | 联合概率 |
| --- | --- | --- | --- |
| WAIT | 无 | 等待下一个事件或超时唤醒 | `p(WAIT)` |
| PLAY | 牌、位置、延迟 | 原有延迟规则 | `p(PLAY) p(card) p(cell|card) p(delay|card,cell)` |
| ABILITY | 确切的己方单位及技能 | 决策后下一原生 tick | `p(ABILITY) p(source|ABILITY)` |

技能第一版即时释放，不另选位置或延迟；只支持原生已验证的无目标技能。
合法候选来自同一份原生观察，包含费用、冷却、次数及确切的来源身份。
采样与 PPO 重算使用归档的同一候选集合。提交前再次核对原生合法性，
技能来源死亡、冷却状态改变或费用不足时取消该决策，排除学习。

只有原生 `action_executed` 回执确认来源、技能身份和
`fair_ability_activation_exact` 后，才产生技能事件。排队回执不触发事件。
成功释放会唤醒双方重新决策，打断尚未提交的出牌延迟计划；拒绝只唤醒
受影响的一方重试。动作 ID 去重，真实终局优先。技能费用随执行回执保存。

记录增加 `ability_candidate`、`ability_source_entity`、`ability_vocab_id`、
技能 log probability 和 entropy。技能行的 card/cell/delay 均为 -1，
其概率项与熵为 0。PPO、参考 KL、预检、批量推理和重载均识别技能分支。
四个采样温度及四随机数协议保持兼容；技能来源复用互斥的 card/source 槽。

## 权重升级与训练

旧模型默认 `ability_actions=False`，旧检查点合同哈希与权重保持兼容。
显式升级生成 `event-checkpoint.v4`，与旧动作语义隔离：

```sh
python tools/event_migrate_abilities.py --source OLD.pt --output NEW-ability-init.pt
```

输出必须是新路径，保存后回读验证检查点合同及 SHA。迁移逐参数保留模型、AdamW 动量、训练进度、KL 系数、
已有分项价值头状态和奖励配置；仅新增技能 gate/source 头及空优化器状态。
这不是一次训练更新。迁移输出标记为待微调、待评估，普通加载拒绝将其视为
已完成训练的策略。

首次采集与更新分别使用 `tools/event_rl_rollout.py` 和
`tools/event_rl_update.py` 的 `--initialization-only`。常驻 learner 的 argv
同样支持该标志；首次 verifier 配置使用 `initialization_only: true`。
初始化推理产物必须显式导出为 `export_inference(..., initialization_only=True)`，
用途标记同时绑定产物与任务描述。实际非零学习率更新后解除初始化限制，
仍保留待评估标记。历史出牌策略与技能策略可在同一推理服务中分开路由；
其观察、时序及除技能开关外的结构合同仍须一致。

参考 KL 要求同一动作合同；如使用旧基线做 KL 锚点，应另存其技能架构迁移版，
并在新任务配置中记录新身份。既有正式训练和冻结实验无需原地修改。

## 适用边界与验收

- 必须使用含英雄技能的卡组采集后续训练数据。现有五套基础卡组没有技能覆盖。
- 原有溢费/塔血训练采集器仍保留五卡组验证门槛；扩展英雄卡奖励训练前，
  需验证技能实际扣费时点和英雄相关圣水机制。当前改动没有放宽这项门槛。
- `demo_event_selfplay.py` 使用同一技能采样器，并导出来源及技能身份。
  `replay_driver.py` 的出牌改时回放明确拒绝技能轨迹，避免静默遗漏技能；
  应使用保留来源身份的原生回放链路。
- 屏幕实时接口缺少已验证的技能状态及回执，继续屏蔽技能候选。
- 回归测试覆盖源单位、非法候选、概率重算、PPO 更新、分批推理、回执去重、
  取消过滤、双方事件唤醒、换边、检查点迁移与优化器恢复。
- 原生环境桥接采用脚本化引擎夹具验证；实际引擎对局及新权重技能训练需另行验收。

动作空间实现阶段只升级代码和测试；后续技能头 IL 阶段的状态另见下节。

## 验证记录

2026-09-25，本地相关回归 **803 项全部通过**（107.43 秒）：

```sh
.venv-live/bin/python -m pytest -q native_runner/tests/test_event*.py \
  native_runner/tests/test_battle_env_rich_telemetry.py \
  native_runner/tests/test_native_ability_action.py \
  native_runner/tests/test_native_ability_exact_source.py \
  native_runner/tests/test_live_adapter.py \
  native_runner/tests/test_live_consistency.py \
  native_runner/tests/test_live_runner.py \
  native_runner/tests/test_live_latency.py --tb=short
```

8 条警告来自旧检查点初始化提示和已有测试中的张量转标量；没有失败项。
此结果覆盖脚本化原生桥接与本地训练/推理测试，不代表实际英雄对局已经验收。

## 冻结旧策略的技能头 IL

2026-09-25，按用户要求，仅训练 `ability_gate` 和 `ability_query` 的四个参数张量。
入口为 `tools/build_event_ability_features.py` 和 `tools/train_event_ability_heads.py`。
父模型为交战结算奖励分支的新 1753，任务记录在
`artifacts/ability-head-il-20260925/README.md`。已核对真实父文件并生成独立初始化
检查点；实际 IL 是否完成以该任务的训练报告为准。

旧骨干、WAIT/PLAY、card/cell/delay、价值头及 buffers 全部冻结，编码器使用 eval。
技能 gate 只学习“立即释放/本 tick 不释放”，WAIT 和 PLAY 的相对概率不作为
模仿目标；来源头仅监督原生已确认的来源。独立 AdamW 只接收四个新参数，
保存后逐项回读旧张量、原 PPO 优化器、KL、约束及训练进度的稳定哈希。
技能可用时新分支仍会改变整体动作分布，因此参数保持不变不等于实战表现已验证。

原始回放只有技能标记时，先重建原生局面。训练帧为源命令时点 P，执行回执为
P+1；拒绝技能遗漏、排队回执、来源歧义、截断候选和终局不符的整场数据。
按整场回放划分训练/验证，双方及所有时点不跨分区。特征缓存绑定父模型 SHA。
目前唯一来源正例不产生多来源选择学习信号；报告独立列出该覆盖情况。
负例来自技能可用且专家下一 tick 未释放的时点，采样比例不代表自然对局技能频率。

IL 以验证集损失选择检查点；独立保存并保留待对局评估标记，不自动替换正式权重。
当前完整代码回归为 828 项通过，随后技能时点修复定点验证为 26 项通过。
