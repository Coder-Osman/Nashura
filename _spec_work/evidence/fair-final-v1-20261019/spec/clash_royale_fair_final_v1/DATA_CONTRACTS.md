# FAIR AlphaZero 数据契约与接口约束 v1.0

**性质**：实现所应满足的逻辑Schema/断言草案；字段名供规范参考，**并非声称现有代码具有这些API**。仅正常决策帧进入训练Replay，不采集中间pending状态。

## 1. 词汇与身份

- `FullAction`: `WAIT` 或 `ACT(card_catalog_id, x_index∈[0,17], y_index∈[0,31], delay_slot∈[1,30])`；delay_ms = 100×delay_slot。保留0ms内部ID但对外严禁执行。
- `DecisionBoundary`: 上一指令已执行、抢占或失效，当前玩家在合法观察时刻可提交新指令的状态。
- `InformationState`: 玩家当前FAIR可见事件历史、己方可见私有状态和控制状态的确定性编码；不包括模拟真隐藏状态。
- `RootDeterminization`: 在每个根一次性从经验先验且规则兼容的后验抽出的补全世界；仅用于模拟器。
- `BehaviorAction`: 实际提交给引擎的完整动作；不同于A的teacher标签和搜索目标。
- `PolicyTarget`: 仅Full根有效的稀疏访问分布 `{action_id,visits}`，`sum(visits)=128`。
- `SearchProtocolID`: 先验/候选/根噪声/PUCT/深度/边界/对手机制的规范身份。
- `DirectControllerID`: checkpoint、头温度、抽样与候选合法mask身份。
- `Episode`: 从真实比赛初态至完整终局的双方轨迹及证据，不是某次模拟路径。

## 2. `FullAction` API与保证

规范性接口：

```text
encode_action(FullAction) -> int
 decode_action(int) -> FullAction
is_legal_at_decision(I, FullAction) -> bool
has_legal_completion(I, prefix) -> bool
sample_joint(I, policy_version, temperature_vector, rng) -> (FullAction, logp)
log_prob_joint(I, FullAction, policy_version, temperature_vector) -> float
```

强制断言：

1. `decode(encode(a))==a`，同一catalog与坐标语义下无歧义；非法槽ID不可执行。
2. Mode=WAIT没有Card/Position/Delay条件因子；ACT必须四级概率都合法。
3. 每个合法前缀存在≥1合法后缀；计算未来合法性不能读取未知真实未来。
4. 使用完整枚举的小动作玩具环境验证 `Σ_{legal a}p(a|I)=1`、`log_prob`与多级采样的频率吻合。
5. `teacher_old61 → new30`明确两种不同含义：原完整联合分布截断 vs 逐级条件mask；选定后写入 `adapter_id`，并测试贪心动作与分布数值。
6. 卡牌身份必须用稳定catalog ID，不能用动态手牌序号冒充实体身份；换边坐标变换可逆。

## 3. 单次真实对局 `AZJointEpisode/v1`

建议的逻辑结构：

```yaml
schema: AZJointEpisode/v1
status: COMPLETE                  # COMPLETE / QUARANTINED / INVALID
match_id: "..."                   # 由赛制、种子、座位、对手、配置组合生成的稳定ID
pair_seed_id: "..."
attempt_id: 0                      # 同原种子最多3次；不能与另一次结果混成独立样本
engine_protocol_sha: "..."
action_protocol_sha: "..."
fair_protocol_sha: "..."
root_belief_prior_sha: "..."
checkpoint_blue_id: "..."
checkpoint_red_id: "..."
controller_blue_id: "..."
controller_red_id: "..."
opponent_pool_version: "..."
rng_stream_ids:
  world: "..."
  behavior: "..."
  opponent: "..."
  candidate: "..."
  root_noise: "..."
  engine: "..."
  replay: "..."
  evaluation: "..."
result:
  game_winner: blue             # 或red/draw，来自引擎原始终局
  game_score_evidence: "..."   # 终局来源、完整校验记录、裁决规则
  last_game_tick: 0
decision_rows:
  - row_id: "..."
    game_tick: 0
    acting_player: blue
    information_state_ref: "..."   # 仅FAIR编码引用
    observation_contract_sha: "..."
    pending_status_at_boundary: none
    behavior_action_id: 1
    behavior_controller_id: "..."
    search_mode: FULL            # FULL / FAST / NONE
    search_result_ref: "..."    # FULL必有，FAST可有无Policy的运行统计
    policy_target_ref: "..."   # FULL必有；FAST必须为null
    outcome_wdl: WIN            # 从本行动者视角导出
    outcome_aux_ref: null       # 仅决策帧、如启用终局辅助监督
    event_evidence_ref: "..."
```

- Episode中的各决策行只允许同局归属同一训练/验证/测试/确认划分；`result`缺失则不得进入Replay。
- 存真实隐藏种子、对手真牌序等字段时必须位于**隔离审计区**，不能通过正常Observation序列化、网络缓存或actor检索泄漏。
- `FULL`的Policy标签来自**当前这一行己方根**；绝不从搜索树中间其他根复用为真实监督而不带独立合法决策身份。
- 同局成功完成后，重复收到完成通知必须幂等去重。

## 4. `JointMigrationEpisode/v1`

独立于正式Replay，额外字段：

```yaml
schema: JointMigrationEpisode/v1
source_checkpoint_sha: "e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015"
adapter_sha: "..."
old_vocab_identity: "..."
new_delay_identity: "100-3000ms/100ms"
student_checkpoint_id: "..."   # 第二轮状态补充时存在
split_id: train                 # 预先按完整局决定
rows:
  - fair_information_state_ref: "..."
    executed_action_id: 1
    teacher_greedy_action_id: 2
    teacher_conditionals_ref: "..."  # 条件soft目标，不得全凭硬argmax
    teacher_wdl: [0.7,0.1,0.2]
    eventual_outcome: WIN
    teacher_policy_mask_semantics: "..."
```

第一批1024局(896/64/64)，两轮各128局(96/16/16)。不得把`executed_action`与`teacher_greedy_action`混写成唯一`action`，否则DAgger式状态补充无法诊断偏离。

## 5. `SearchResult/v1`

```yaml
schema: SearchResult/v1
root_fair_state_sha: "..."
world_sampler_sha: "..."
world_sample_commitment: "..."      # 只含审计所需身份/哈希，不含根可读真牌序
search_protocol_id: "..."
opponent_controller_id: "..."
valid_root_simulations: 128
invalid_attempts: 0
candidate_count: 0
candidate_actions:
  - action_id: 1
    proposal_type: CARD_ELITE      # 或 SPATIAL_PEAK / TIME_NEIGHBOR / RANDOM / WAIT
    original_joint_logp: -1.2
    prior_projected: 0.18
    prior_search: 0.16
    prior_card: "..."
    visit_count: 12
    q_root_perspective: 0.14
boundary:
  soft_event_cap: 6
  soft_time_cap_ms: 10000
  extension_policy_id: "..."
  rollback_count: 0
  unfinished_edge_backup_count: 0
search_cost:
  engine_steps: 0
  network_evals: 0
  wall_time_ms: 0
```

强制检查：

- `sum(visit_count)==valid_root_simulations==128`，且各count非负整数。
- 每节点所有候选一开始构建为固定`C_max`并确定静态先验；动态开放子集时不能再次重归一化已定义的prior。有效`K_max`须至少覆盖WAIT加所有当前合法卡牌，否则拒绝配置或上调K。
- 候选完整合法且唯一；任何根模拟不能没有完成根边就计入128。
- 搜索先验投影 `sum(prior_projected)=1`；如有合法ACT，WAIT/ACT原边际和已覆盖卡牌边际在数值容忍度内守恒。
- 根噪声只在训练根启用；正式确认和固定搜索增益实验为无噪协议。
- 计算根视角Q时，保留`root_player_id`和`value_perspective_id`，每个值含明确方向，不按事件计数翻号。
- `pending`未处理完的当前边不得备份：`unfinished_edge_backup_count==0`。无效根尝试达到上限触发隔离，不允许填零凑满128。
- 单根world identity唯一固定；根不读取真实隐藏牌组。记录其哈希只能用于审计，不得提供给策略输入。

## 6. Policy监督、合法mask和稀疏目标

`policy_target`标准：

```yaml
source: FULL_MCTS_VISITS
valid_simulations: 128
entries:
  - {action_id: 1, visits: 64}
  - {action_id: 2143, visits: 32}
  - {action_id: 8901, visits: 32}
```

`π_i=N_i/128`，`L=-Σ_i π_i log pθ(action_i|I)`。不需要存完整142,849维FP32标签；同时不允许把未选动作的0访问解释成已验证差动作。训练时必要的合法条件掩码由FAIR决策帧和契约版本重建，或者压缩存储并提供精确比较的hash验证。`FAST`行必须显式`policy_target=null`，**绝不能**把16访问除以128伪造Policy目标。

统计输出：`mode_mass`, `card_mass`, `position_entropy`, `delay_entropy`, `candidate_recall_proxy`, `unused_legal_mass`, `prior_projected`, `search_noise`, `actual_act_frequency`。训练和部署不同温度应独立命名，不得模糊为一个`policy_prob`字段。

## 7. Replay分层与统计不变量

索引键：`(split, window, game_id, acting_player, actor_id, opponent_id, search_protocol_id, mode=FULL/FAST, revealed_flag)`。先按完整对局层采样/配额，再按决策行采样。Policy和Value分开计量有效样本数，禁止混合未完成局/无终局样本。

Dataset元数据：`dataset_manifest_sha`, `split_seed_namespace`, `retention_window`, `max_rows`, `source_quota_config`, `replay_cursor`, `sampling_rng_state`, `admission_rule_version`。近期窗口或配额改动必须独立版本化。

训练/验证/测试/确认划分必须在独立种子命名空间内预先固定，且同一局所有帧只能进入一个划分。允许版本化的Candidate行入Replay，但该行可按来源撤回，不要求Candidate最终晋级。

## 8. Checkpoint / Controller / Trainer

规范性checkpoint条目：

```yaml
schema: AZJointCheckpoint/v1
network_sha: "..."
model_architecture_sha: "..."
encoder_catalog_sha: "..."
fair_observation_sha: "..."
weights_path: "..."
optimizer_sha: "..."             # learner私有，不属于公开champion权重
gradient_scaler_sha: null
trainer_rng_sha: "..."
trainer_replay_cursor: "..."
source_a_sha: "..."
migration_adapter_sha: "..."
active_champion_sha: "..."
search_protocol_sha: "..."
direct_controller_config_sha: "..."
trainer_state: RUNNING            # RUNNING / PAUSED / QUARANTINED
```

`LearnerCheckpoint`随SGD频繁改变，`CandidateCheckpoint`冻结并单独评测，`ChampionCheckpoint`不可变并原子发布。直出温度与搜索协议绑定在控制器manifest中；官方要求**同一权重SHA**分别满足搜索、直出。

## 9. 训练及调度状态机

```text
CAPTURED -> VALIDATED -> EPISODE_COMMITTED -> REPLAY_ADMITTED -> TRAINED
                                            -> CANDIDATE_FROZEN
                                            -> PRESCREENED
                                            -> CONFIRMED_BOTH
                                            -> CHAMPION_PUBLISHED
```

任何阶段错误进入`QUARANTINED`并记录原因；网络/推理/引擎错误不得被“正常空操作”吞掉。允许正常计划抢占仍属于`VALIDATED`。定期快照包括优化器、RNG、游标、数据范围、分段对局证据与原子完成标志；同原比赛种子不同attempt的对局不得作为独立评估样本重复计数。

## 10. QA断言清单

- `Same FAIR input + same belief RNG + different true hidden state -> same pre-event action`。
- `Candidate prior`在小合法动作测试上计算期望边际；`ACT`质量不得因少数候选丢失，`WAIT`不得错误放大。
- `同tick`双方冻结信息后统一提交；新增触发会抢占未到期预约；到期同tick旧预约先结算。
- `Value`只接受DecisionBoundary；搜索截断不可调用中间pending状态WDL。
- 根`N`合计严格128；无效/截断/恢复失败不能凑访问。
- `sample_joint`和`log_prob_joint`一致；`pθ`为完整联合概率；迁移旧61→新30截断语义明确。
- 类别WDL终局标签从该行行动者视角生成；同一局两侧标签一致但符号相反（平局皆为0）。
- `search_controller_id`、`direct_controller_id`可通过manifest完整重建，正式评测两者的`network_sha`相同。
- 重要统计均可按完整对局、对手、FAIR揭示前后、Card、Position、Delay分组。
