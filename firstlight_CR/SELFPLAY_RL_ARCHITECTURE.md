# Nashura · 事件模型强化学习架构（三专精对抗联盟）

> 项目代号 **Nashura** = **Nash**（纳什均衡）+ **shura**（修羅，阿修羅道的无尽争斗）。
>
> - **Nash**：多智能体对抗的终局概念。三个专精（石头人 / 天狗 / 皮卡锤）是非传递的"剪刀石头布"结构，不存在"把某一专精练到最好"，只存在三者互相无法单方面改进的均衡点；§8 里"矩阵严重非传递"的监控，本质就是在盯有没有偏离或绕过均衡。
> - **shura**：自博弈的形态——没有老师，只有对手；训练不会终结，只是不断刷新快照接着打。
> - 名字中间的 **shu** 也延续了"守破離"的血脉：守（IL 模仿，学范式）→ 破（RL 自博弈，离范式）→ 離（专精自成一派）。IL checkpoint 就是"守"的坐标系——基线初始化与永久锚点，偏离了多少必须可测。
>
> 与 `SELFPLAY_ARCHITECTURE.md` 的区别：那一版是"IL 自博弈"（把 rollout 当模仿数据），**本版是真正的 RL**，IL checkpoint 只作为**基线初始化与锚点**。

## 0. 摘要

- **模型**：事件模型、**快照契约**（`CR_EVENT_MODEL_SNAPSHOT_ONLY=1`）、容量取 **1×（13M）**——2×/3× 对照已证无增益且更慢更吃显存。
- **动作建模**：`p(gate)·p(card)·p(cell|card)·p(delay|card,cell)`——延迟头已改为条件在所选**格子**上（不再与格子条件独立），模型侧采样接口见 §6.1。
- **算法**：PPO + GAE(λ)，**逐决策** γ=0.999 / λ=0.95（现状；稀疏奖励下 λ 是首要标定对象，见 §4.2）。
- **起点**：三个专精用**同一份 IL checkpoint** 初始化后各自独立演化（事件模型的卡牌头是在"自己牌组的行"上打分，与牌组无关，所以一份权重可服务三个牌组）。
- **奖励**：**终局输赢 + 终局幅度**（±1/0 加 β·margin，margin = 皇冠差 + 加权塔血量差）。不加逐步塑形、溢出罚、出牌奖励；非终局步奖励仍恒为 0。
- **幅度不改变稀疏性**：它只区分"顺利取胜 vs 艰难取胜"，逐步信用分配仍全靠 critic。
- **信用分配靠价值函数而非奖励**：终局信号是唯一的真值来源，critic 必须学出该终局回报的期望，逐步优势由它提供。
- **硬前提**：对局必须**打到自然结束**（`end_tick` 覆盖全局，不能用 2490 截断）——截断局 reward 恒为 0，该条轨迹不含任何胜负信息。
- **防坍缩**：冻结快照池 + 永久 IL 锚点对手 + 对 IL 的 KL 牵引（衰减）+ ACT 率作为一等指标。
- **算力**：引擎 108 槽（5 台机，纯 CPU）× 3 个学习者各 8 卡（共 24 卡），余下 15–16 卡做评估/超参/旧线。

## 1. 上次为什么垮，这次怎么绕

上次联盟坍缩的链条（已复盘）：**1x 奖励的大头是终局一次性势能结算（稀疏）→ 过渡期几乎全是 WAIT 样本 → critic 学不出状态差异（处处 ≈ −2.2）→ ACT 拿不到正优势 → PPO 把 ACT 压死**。

复核旧代码（`tools/league_train.py`）发现当时的奖励远不止一项，而是五个手写项叠在一起：

| 旧项 | 默认值 | 位置 |
| --- | --- | --- |
| 势能塑形 `shaping_beta` | 0.5 | `league_train.py:75` |
| 圣水溢出罚 `overflow_beta × waste` | 0.05 | `:76, :443-444` |
| **每次出牌送分 `reward_play`** | 0.01 | `:77, :438` |
| 强制出牌概率 `FORCE_ACT_PROBABILITY` | 0.25 | `:79, :215` |
| gate 采样温度 | 1.0 | `:78, :118` |

三项奖励合起来是 `r = rewards[owner] − 0.05·waste + 0.01·played`（`:444`）。本版把这些**全部清零**（见 §11-5 已落地）：逐步塑形、溢出罚、出牌送分都不加，奖励只由终局输赢与其幅度构成；`FORCE_ACT_PROBABILITY` 一并去掉（用户此前已明确否决强制 ACT，且它是探索补丁不是奖励）。

| 对策 | 具体做法 |
| --- | --- |
| 只留输赢 + 终局幅度 | 关掉全部逐步塑形项；r_T = ±1/0 + β·margin，非终局 r = 0 |
| 幅度不得越权 | 构造期强制 β·8 < 0.5，保证胜 > 平 > 负三段互不相交 |
| 价值扛信用分配 | 稀疏奖励下 critic 是唯一的细粒度优势来源 → 按 Φ 或终局回报回归热启动 |
| 探索不熄火 | 采样（非 argmax）+ 熵系数；ACT/WAIT 比例当一等监控指标 |
| 不忘记怎么打 | IL checkpoint 常驻对手 + KL(p‖p_IL) 罚项，系数随时间线性衰减 |
| 不自激震荡 | 每个专精保留最近 K 个冻结快照作为对手 |


## 2. 分层与机器分配

| 层 | 职责 | 机器 | 资源 |
| --- | --- | --- | --- |
| 引擎层 | rollout（对局 + 观测落盘） | 全部 5 台，108 槽 | 纯 CPU，不占卡 |
| 数据层 | 分片汇聚/分发 | `.37`（`/data` 666G） | 各机暂存 + `--remove-source-files` 推送 |
| 学习者层 | 3 个专精各一个 PPO 作业 | `.34` / `.37` / `.39` 各 8 卡 | 24 卡 |
| 客场层 | 评估、快照对局、超参扫描、V4 旧线 | `.35`（7 卡）/`.41`（8 卡） | 15 卡 |
| 看护层 | 引擎健康/冷启/重武装 | 各机本地 | 复用 `event_capture_watchdog.sh` 的分层恢复 |

`.34` 自己也有 24 个引擎槽：**只在引擎产量不足时开**（训练是 CPU/IO 密集，与引擎抢核会双输）。

## 3. 数据契约（RL rollout 分片，新 schema）

现有 `event_cache` 分片是给 IL 的（观测 + 老师标签）。RL 要另存一路，沿用同一压缩/校验容器（`write_event_cache_shard`）：

每行（一次决策）：
- 观测帧（不变，快照契约）
- **实际执行的动作**：gate /（ACT 时）card、cell、delay 的采样值
- **该联合动作在当前策略下的 log-prob**（重要性比与 PPO 比值都靠它）
- **策略版本 id**（用于离线校正与"行为策略 ≠ 目标策略"检测）
- **奖励 r_t**、**Δticks**（到下一决策/终局）、done
- **归属方**（这一行是"我"还是"对手"）——只有归属本专精的行才进它的损失

分片摘要附加：对局结果（胜负/皇冠/终局 tick）、双方专精 id 与牌组、版本号——用于优先级采样、胜率统计与离线筛数据。

## 4. 奖励设计：终局输赢 + 终局幅度

```
r_t = 0                                     (非终局，仍然是稀疏的)
r_T = 输赢(±1/0) + β · M                     (只在终局结算)
M   = 皇冠差 + Σ 加权塔血量损失差             (王塔权重 ×3)
```

**先说清楚一件事：这没有让奖励变稠密。** 非终局步依然恒为 0，所以 §4.1、§4.2 的稀疏性问题**原封不动成立**——变的只是终局那一个值的幅度：从"所有胜都一样 = 1"变成"顺利取胜 > 艰难取胜"。它的作用是把 critic 的回归目标从二值变成有梯度的一维，而不是提供逐步信用分配。

### 4.0 幅度项的实现

环境里**本来就有**这个计算：`_potential` 的非 princess-only 分支就是"皇冠差 + 加权塔血量损失差（王塔×3）"。新加的 `_terminal_margin()`（`battle_env.py`）复用同一套权重，但**刻意在终局求值**——这与 PBRS 必须把终局势能置零的规则相反，代码原注释正是在警告这一点（"carrying the crown/tower potential into the terminal reward … changes the optimal policy"）。**这次是有意为之**：我们就是要奖励"赢得漂亮"。

开关 `terminal_margin_beta`（默认 0.0，即关）加在 `BattleEnvV1` 构造参数上而**不是** `EnvironmentConfigV1` 里——后者是内容寻址的（ruleset manifest 会校验相等，`:451-453`），现有的 `princess_only_potential`/`truncation_terminal_potential` 也正因此留在外层。

**安全性有硬约束**：margin 的绝对值上限是 3 皇冠 + 5 塔权重（两座公主塔 + 王塔×3）= **8**（常量 `TERMINAL_MARGIN_MAX`）。构造函数强制 `β·8 < 0.5`，于是

| 结果 | 奖励区间 |
| --- | --- |
| 胜 | [0.6, 1.4] |
| 平 | [−0.4, 0.4] |
| 负 | [−1.4, −0.6] |

三段互不相交——**无论塔被拆成什么样，胜永远高于平、平永远高于负**。这条正是"奖励不能反过来教它挑肥拣瘦"的保证：β 取默认 0.05，上限 0.0625，超了直接抛错。

实测数值（测试锁定）：3 冠全拆、自家满血 → M=8 → **1.40**；1 冠但自家塔被打残（两公主 0.9 + 王塔 0.75×3）→ M=−2.05 → **0.8975**。两者都是胜，差 0.5，这就是"顺利 vs 艰难"。

边界处理：只在 `terminal.ended`（真实胜负）时结算，截断局没有 margin——截断本来就没有赢家。

### 4.1 硬前提：必须打到自然结束

这是纯输赢奖励**唯一的致命坑**。旧配置 `end_tick = ONE_X_MAX_TICKS = 2490`（`league_train.py:52`），而实测自然终局在 tick ≈ 3352、原生终局 deadline 是 `NATIVE_FINALIZATION_DEADLINE_TICK = 7200`（`match_factory.py:44`）。截断时 `_terminal()` 返回 `TerminalV1()` 默认值 → `ended=False`、`result_by_owner=(0,0)`（`battle_env.py:1409-1410`，`contracts.py:2622`）→ **整局每一步 reward 恰好为 0**。后果：

- 一局被截断 → 这条轨迹**不提供任何关于胜负的信息**。GAE 在截断处切断迹（`learning.py:211`），而 `terminated=False` 仍会 bootstrap（`:210`），所以残留的梯度只是"V(s) 与 γV(s') 自洽"这种退化拉扯——把价值往互相将就的方向拽，**学不到任何东西**。若 critic 初值为 0，则连这点梯度都没有（delta 全 0）。
- 旧联盟的策略几乎全程 WAIT → 没有塔会倒 → 每局都在 2490 截断 → 这本身就足以让"只留输赢"的奖励彻底失效。**所以 `end_tick` 与 `max_battle_ticks` 都必须改成 7200**（两者需相等，否则 reset 报错），让每局都走到自然胜负。

### 4.2 稀疏化的连带决策（都不是"加奖励项"）

1. **GAE 的 λ（以及 γ）——这是真正的旋钮，不是折扣 τ**。核实结果推翻了我先前的判断：训练器的优势估计走 `PPOConfigV4(gamma=args.gamma, gae_lambda=args.gae_lambda)` → `generalized_advantage_estimate`（`learning.py:177`），是**逐决策** γ=0.999、λ=0.95（`league_train.py`），**不做 tick 折算**；而 `event_config.discount(τ=1200)` 在整个仓库里**没有任何调用点（死代码）**。量化：γ^240 = **0.787**，终局 ±1 传到开局仍剩 79%，**不存在"指数压扁"问题**；但 GAE 迹的有效长度是 (γλ)^n——n=60 时 **0.043**、n=120 时 **0.0019**。所以终局奖励下开局的优势几乎只能靠 critic 的 bootstrap 提供（这正是下一条为什么重要）。若 P2 发现学不动，**先把 λ 提到 0.98–0.99**，而不是去找奖励项。
   ⚠️ **但这条现在传不进去**：master 的 `spawn_trainer()`（`league_train.py:845-871`）只转发 `--min-lanes/--epochs/--learning-rate/--gamma/--trainer-cycle-seconds/--max-lanes/--autocast/--policy`，**`--gae-lambda`、`--entropy-coefficient`、`--lane-minibatch`、`--sequence-chunk-steps`、`--keep-*` 都没转发**——master 层传 `--gae-lambda 0.98` 会被静默忽略，训练器子进程仍用默认 0.95。**这是必须修的 bug（属于 P0）**，否则本版最重要的标定旋钮根本拧不动。
2. **价值预热**：奖励不含任何逐步信息后，**critic 是细粒度优势的唯一来源**。IL 阶段 `event_il_loss` 没训过 value 头，必须先用 Φ（或直接回归"对固定对手的胜率"）热启动，否则等价于从零学 P(win)。
3. **熵系数与采样温度**：`--entropy-coefficient 0.05`、`--explore-gate-temperature 1.0` 保留（它们是探索机制、不进奖励）。稀疏奖励下探索更容易塌成确定性 WAIT，这两个值是 P2 的标定对象。

排除项：`SEGMENT_REWARD_SPEC.md` 的 Q1–Q4（建筑是否计入"有兵"、王塔血量是否计入等）**在本版不再适用**——那些都是塑形势能的参数，奖励只剩输赢后整份 spec 与本版无关。


## 5. 联盟结构

- **成员**：3 个当前专精 + 每个专精 K 个冻结快照 + **IL 锚点（永久）**。
- **对阵**：每轮采样 (A,B)，各用自己牌组；不同专精天然异组，同组镜像只在"专精 vs 自己的快照"时出现。
- **归属**：一场对局出两个视角，**只有学习者自己那一侧**的决策进该专精的损失；对手侧只作上下文。
- **刷新**：每 30–60 分钟把 rollout 策略换成最新 checkpoint（近 on-policy）；快照池每小时补一批。
- **评估剧本**：固定种子 × 固定卡组 × 每对组合，每小时跑一次胜率矩阵；同时记录 ACT 率、溢费率、平均局时长、**value 与实际胜负的相关性**（纯输赢奖励下这是 critic 是否在工作的唯一体现）。

## 6. PPO 细节

- **联合动作 log-prob**：动作是 `(gate, card, cell, delay)`，联合按模型的自回归分解累加：

  ```
  log π(a) = log p(gate) + 1[ACT]·( log p(card) + log p(cell | card) + log p(delay | card, cell) )
  ```

  现有 IL 损失是分头 CE，**不提供联合 log-prob**，这是必须新写的核心件（含掩码内归一化）。模型侧接口已就绪，见 §6.1。
- **优势**：GAE(λ)，**逐决策** γ=0.999、λ=0.95（`PPOConfigV4` → `generalized_advantage_estimate`，不做 tick 折算）。参照 `native_runner/training/v4/ppo.py`、`distributed_ppo.py` 移植到事件动作空间。
- **约束**：PPO clip + 熵正则 + KL(p‖p_IL) 罚（衰减）。**这三项都作用在策略侧、不进奖励**——奖励只有终局输赢与其幅度。（KL 罚是防遗忘的正则，若认为它也算"额外项"，可只保留前两项并把 IL 锚点仅用作对手。）
- **分布式**：每专精一个 8 卡 DDP；rollout 由 108 槽异步生产，走已打通的 rsync 桥。
- **显存**：观测场景是显存主因（实测每行约 22 MiB 激活），序列变长时用 `--grad-accum`（已在远端训练器实现并验证）或 batch 2。

### 6.1 模型侧的采样/解码接口（已落地）

事件模型的动作是**条件的级联**，不是两个独立边际：

```
encode(batch, state) → context          # backbone 只跑一次（贵）
heads(context, card_row=?, cell=?, cell_mask=?) → gate/card/cell/delay logits（便宜，可反复调）
```

`heads` 的 `card_row` / `cell` 决定下游头条件在什么上；省略则退回模型自己的 argmax。**解码器在模型外解出的合法性选择要回传进来**，否则延迟就不是条件在实际打出的那个落点上。演示脚本已按此改成"编码一次 → 选卡 → 用该卡重算头 → 选格 → 用该卡该格重算延迟"。

采样器要按同样顺序做：`gate → card → cell → delay`。这个顺序既精确（自回归分解的 ancestral 采样 = 联合的精确样本，比 argmax 级联更强——后者不保证取到联合众数），也是被强制的：**落点掩码按手牌槽位索引**（`placement_masks[str(hand_index)]`），格子合法集只有在卡选定后才存在。

采样实现可直接照搬 V4：`_distribution(logits, mask, temperature)` → `Categorical`，`_choice(distribution, sample)` 一个开关统管所有决策点；`ppo_rollout_sampling` 默认为 `True`（部署用的 `act()` 才是贪心）。**别漏了温度**：V4 有 `_check_temperatures` 强校验收集温度与训练时一致，事件线要比 V4 多记一个（gate/card/**cell**/delay 四个），否则 PPO 的比率不可信。

## 7. 时间与算力分配

| 阶段 | 时长 | 内容 | 通过判据 |
| --- | --- | --- | --- |
| P0 打通 | 2–3 天 | 奖励与自然终局已落地（§11-5）；其余见 §10 的缺口清单与最短路径（先修两个既有 bug → 采样/log-prob/掩码/value → RL 分片与镜像 rollout → 最小版 PPO） | 一场对局→分片→PPO 更新→评估全链路可跑，且**截断率 ≈0、reward 非零** |
| P1 测基线 | 半天 | IL checkpoint 在三个牌组下的胜率矩阵、ACT 率、溢费率、平均局时长 | 拿到基线数字（后续一切对着它比） |
| P2 单专精 | 1–2 天 | 先石头人镜像自博弈（`SELFPLAY_MATCHUPS`）跑通，再对 IL 锚点验证稀疏奖励是否可学、标定 λ/熵/采样温度/β | 对锚点胜率显著 > 50%，且 ACT 率不退化；若出现坍缩签名则回 §4.2 调参而非加奖励 |
| P3 三专精联盟 | 持续 | 3 学习者并行 + 联盟刷新 + 快照池 + 每小时评估 | 矩阵不坍缩；各专精对锚点 > 55% |
| P4 固化 | 每天 | 固定剧本评估 + 可视化对局定性检查 | 选型/回滚 |

**吞吐**：108 槽，保守估 5,000–26,000 场/h（待标定；引擎侧是唯一瓶颈），每场约 240 个决策样本 → 120 万–600 万决策样本/h。PPO 一轮取其中 1–2 小时的数据即可，GPU 仍大量空闲 → 余下卡做评估/超参/旧线。

⚠️ **上面的吞吐要在自然终局口径下重标**：旧数字来自 2490 tick 截断（≈124 s/局），而只留输赢必须打到自然结束（实测 3352 tick ≈168 s，上限 7200 tick ≈360 s）→ **单局时长 1.35–2.9×，场/h 同比例下降**。这是纯输赢奖励的直接代价，P0 落地时先测准它。

## 8. 回滚与停止条件

- **⚠️ 截断率必须 ≈ 0**（纯输赢奖励下的第一监控项）。截断局 reward 恒为 0；若截断率显著 >0，等于这部分样本完全不产生梯度，严重时整批数据零奖励、训练原地不动。这是旧联盟"全程 WAIT → 无塔倒 → 每局截断"死循环的直接检测口。
- **reward 非零比例**：每批数据里终局 reward ≠ 0 的轨迹占比。低于 ~90% 就说明胜负判定或终局覆盖出了问题，先查 `end_tick`，别调超参。
- 每侧 **ACT 率**与决策间隔分布：跌破基线一半 → 策略正在退化成"全 WAIT"（上次的坍缩签名）。
- **value 与实际终局回报的相关性**长期接近 0 → critic 没学出 P(win)+幅度，回查价值预热与 λ。
- **胜/负分组的平均 margin**（本项的直接验收指标）：若胜场的平均 margin 长期不上升，说明策略没学会"赢得干脆"，回查 β 是否太小或幅度项是否真在结算。
- 某专精**对 IL 锚点胜率连续 3 次评估下降** → 回滚到该专精上一个快照并暂停其学习。
- 联盟出现"剪刀石头布"死循环（矩阵严重非传递）→ 提高快照池比例、降低刷新频率。

## 9. 三个专精牌组（已落地）

`tools/league_decks.py` 新增 `RL_LEAGUE_DECKS` / `RL_LEAGUE_MATCHUPS`（**追加**，不动旧 `LEAGUE_DECKS`——`league_train.py:49` 的 `POLICY_NAMES = tuple(LEAGUE_DECKS)` 会让旧 IL 联盟从 3 个策略变成 4 个）。`validate_league_decks()` 已通过：三套牌组全部落在受支持卡目录内。

| 专精 | 牌组 | 备注 |
| --- | --- | --- |
| 石头人 | 石头人 / 暗夜女巫 / 精锐野蛮人 / 飞龙宝宝 / 超骑小飞龙 / 大闪电 / 滚木 / 墓碑 | 与旧 IL 联盟同 8 张 |
| 天狗 | **熔岩猎犬** / 气球 / 超骑小飞龙 / 墓碑 / 骷髅飞龙 / 火球 / 电击 / 蝙蝠 | **新增**，IL 阶段零经验 |
| 皮卡锤 | 皮卡 / 战锤 / 刺客 / 皇家幽灵 / 电击车 / 毒药 / 电击 / 小飞龙 | 与旧 IL 联盟同 8 张 |

两点含义：**石头人与皮卡锤的 IL 权重已在这两个牌组上见过经验，天狗是零样本冷启动**——天狗起不来时先怀疑"没见过这个牌组"而不是 RL 算法。克制环成立：地面重推（石头人）↔ 空中重推（天狗）↔ 地面控制压制（皮卡锤）；皮卡对空弱、天狗怕毒药与电击车、石头人怕皮卡切后排。

**首个跑跑看的是石头人镜像自博弈**（用户 2026-09-16 决定）：`SELFPLAY_MATCHUPS = {"golem-beatdown": (("golem-beatdown", "golem-beatdown"),)}`。镜像在 `RL_LEAGUE_MATCHUPS` 里是被拒的（那里是异组对抗），所以单列一张表并让 `_validate_matchups(..., allow_mirrors=True)` 放行；注册表本身与三套牌组一样已通过校验。先用同一策略打自己，是为了在引入对手池之前单独验证训练环路本身。

## 10. 起跑检查（2026-09-16）：事件模型 RL **还不能开跑**

对"石头人镜像自博弈能否起跑"做了逐件核查。结论：**事件模型这条线的 RL 环路基本不存在，缺的不是一个开关而是一条流水线**。已确认存在/缺失如下。

**已就绪的**
- 奖励：终局输赢 + 幅度，已在环境里实现并有测试（§4、§11-5）。
- **动作建模与解码接口**：联合已是 `p(gate)·p(card)·p(cell|card)·p(delay|card,cell)`，延迟头读到所选格子（不再是格↔延迟条件独立）；`encode()`/`heads()` 已拆开，解码器可编码一次后反复调头，并在模型外解出掩码选择后回传给 `card_row`/`cell`/`cell_mask`（§6.1）。参数量 13,013,612 → 13,046,380（+32,768），旧 checkpoint 由 `pad_legacy_delay_head` 零填充加载，行为不变。
- 牌组与镜像对阵表：`RL_LEAGUE_DECKS` / `SELFPLAY_MATCHUPS`（§9）。
- 引擎车队：108 槽 `production_ready`。
- 可复用的**结构性**资产（都在 V4 线，需改指向而非重写）：记录容器与装载（schema-2 packed frames → `_normalize_record` → `assemble_rollout` 组装成 `[T,B]` 补零 lane）、`StoredRolloutV4` / `PPOConfigV4` / GAE 契约、以及 `distributed_ppo_update_v4` 里的 TBPTT 分块、按 lane 均衡的 minibatch、梯度归一与指标输出。

**缺失的（按依赖顺序）**
1. **事件动作空间的采样器**：`EventCardPolicyV1` 有 `encode`/`heads`（可反复调、含 `cell`/`cell_mask`），但仍没有 `_select_action` 那样的采样接口；仓库里唯一下棋的代码是 `tools/demo_event_selfplay.py`，四个头**全是 argmax**。采样式解码必须新写，顺序固定为 `gate → card → cell → delay`（落点掩码按手牌槽位索引，格子合法集只有在卡选定后才存在，所以这个顺序是被强制的；ancestral 采样同时给出联合的精确样本）。可直接照搬 V4 的 `_distribution`/`_choice` + `ppo_rollout_sampling`（默认 True）。
2. **联合 log-prob 与熵**：`log π(a) = log p(gate) + 1[ACT]·( log p(card) + log p(cell|card) + log p(delay|card,cell) )`，含掩码内归一化。事件栈里唯一的 `log_softmax` 是 IL 的 delay NLL。**分维度记录**（V4 已有先例：candidate/target/delay 各自的 log-prob 与熵，外加延迟掩码）**并为每一维记下采样温度**——事件线比 V4 多一维，是 gate/card/cell/delay 四个。
3. **库级合法性掩码**：目前只存在于 demo 工具里（`legal_play_options` + `Decoder`），要提升到 `native_runner/training/` 供采样与重估共用。
4. **价值接出**：`EventPolicyOutputV1` 没有 value 字段（虽然 backbone 每次 forward 都算好了，`context.value` 直接可取）。
5. **RL 记录 schema + 读写**：现有 `EventObservationRecordV1` 是"观测 + 专家标签"，**没有行为 log-prob、采样动作、奖励、Δticks、done、归属方**。RL 需要另存一路。
6. **事件自博弈 rollout 驱动**（镜像、两侧同策略、写出上面那些训练行）。现在没有任何东西从"模型驱动的对局"里产出训练数据。
7. **事件模型版 PPO 训练器**：`distributed_ppo_update_v4` **吃不下事件动作空间**——它要求 `model.ppo_{gate,action,continue}_temperature` 属性（事件模型没有）并调用 `model.evaluate_encoded_action_sequence(...)`（事件模型没实现），且 `StoredRolloutV4` 的 `ActionSequenceV4`（candidate_index/target_cell/delay_bin）表达不了"牌组相对卡行 + 61 档 delay + ACT/WAIT gate"。

**顺带查出的两个既有 bug（会让任何"先跑起来看看"变成误判）**
- **行为似然按错温度记录**：worker 把 gate 温度抬到 `EXPLORE_GATE_TEMPERATURE=1.0` 采样（`league_train.py:114-121`），而 trainer 记录的是**自己**从 checkpoint 载入的 `ppo_gate_temperature`（默认 checkpoint 为 0.2，`:684-686`）。`_check_temperatures` 拿模型和 rollout 比、两边都是 0.2，**恒成立**，于是 PPO 比值从第一个 minibatch 起就在拿 π_{T=0.2}(a) 去比 π_{T=1.0}(a) 的旧 log-prob。不崩，但重要性比系统性错位。（该做的是让记录写**行为策略真实采样的温度**，trainer 用记录值。）
- **缺 6000 tick 的博弈终点截断**：`league_train.py` 没有 `NATIVE_GAMEPLAY_END_TICK=6000` 的截止（同类 V4 自博弈循环 `async_cluster_self_play.py:946,1040` 有）。6000 之后 `remaining_ms` 被钳到 0、`timeline.phase()` 落到 fallback `"tiebreak"`，即采集到一个旧循环从未采过的状态区制。自然终局约 3352 tick 时不会触发，但没有任何东西兜住它。

**起跑的最短路径（P0 首批）**：先修上面两个 bug（都小）→ 建第 1–4 件（采样、log-prob、掩码、value）→ 建第 5–6 件（RL 分片与镜像 rollout）→ 第 7 件最小版 PPO（只用 1 个 learner、1 个 AVD/6 槽、几十条 lane）验证 loss/胜率方向正确，再谈规模。

## 11. 需要开发的件（已按代码核实修正）

1. **RL rollout 分片**（行为动作 / log-prob / 奖励 / Δticks / 归属；复用 `event_cache` 容器与校验）
2. **联合动作采样 + log-prob**（gate/card/delay/cell）。现状核实：`demo_event_selfplay.py` 的解码**全是 argmax**（gate/card/cell/delay 四处），既没采样也没 log-prob，所以这是必须新写的核心件。可复用的是它已有的**合法性掩码**逻辑（`environment.action_mask` + `placement_masks`），采样器要在掩码内归一化并同时吐出联合 log-prob。
3. **价值输出接出 + Φ 回归预热**。核实结果比预想的轻：`backbone._policy_context()`（`v4/model.py:488`）**每次都无条件算 value**，事件模型 `forward`（`event_model.py:151`）走的正是它——只是构造 `EventPolicyOutputV1` 时丢掉了。所以只需给该 dataclass 加一个 `value` 字段，**rollout 推理开销不增加**。但 IL 阶段 `event_il_loss` 不引用它 → 该头未被训练过，仍需按 Φ 做回归预热。
4. **事件模型版 PPO 训练器**（参照 `training/v4/ppo.py`、`distributed_ppo.py`、`learning.py`）
5. ~~终局奖励（输赢 + 幅度）+ 自然终局~~ **已完成**（`native_runner/battle_env.py` + `tools/league_train.py`）。三处落地：
   - **输赢基项**：`_rewards` 在三个塑形开关关掉后本就返回 ±1/0（`result_by_owner` 被契约限为 `{-1,0,1}`）。
   - **幅度项**：新增 `_terminal_margin()` 与构造参数 `terminal_margin_beta`（默认 0.0，接在 `princess_only_potential` 旁边、**不进内容寻址的 `EnvironmentConfigV1`**），构造期强制 `β·8 < 0.5` 以保住"胜 > 平 > 负"。测试见 `test_battle_env.py::TerminalMarginRewardTests`（5 例：默认关闭时严格等于 ±1、顺利胜 > 艰难胜、β 上限的分离性、β 越界抛错、截断局不结算）。
   - **自然终局**：`MATCH_TICKS = 7200`（env 可覆盖）同时用于 `end_tick` 与 `max_battle_ticks`；训练器默认值 `SHAPING_BETA`/`OVERFLOW_BETA`/`REWARD_PLAY`/`FORCE_ACT_PROBABILITY` 全 0.0，`TERMINAL_MARGIN_BETA` 0.05。
   **所有旧值仍可由环境变量/参数一条命令复现**，`ONE_X_MAX_TICKS=2490` 原地保留作为历史记录。顺带保留 `waste`（圣水浪费）的计算——它不再进奖励，但仍是 §8 的监控量。
6. **联盟调度器**（对阵采样、快照池、IL 锚点、评估剧本、版本管理）
7. ~~天狗牌组定义~~ **已完成**（`RL_LEAGUE_DECKS` + 校验通过，见上）
8. **舰队看护**（capture watchdog 的分层恢复套到 rollout 进程）

## 12. 风险

- **稀疏性未缓解，只是终局值有了层次**：奖励仍是终局一次性，所以上次坍缩那类问题（critic 学不出、ACT 被压死）风险等级不变。缓解全在算法侧而非奖励侧——采样探索 + 熵、价值预热、λ 提到 0.98+、IL 锚点与快照池；监控口是 §8 的截断率与 reward 非零比例。**若 P2 单专精出现"ACT 率持续下滑 + value 与终局回报无相关"的坍缩签名，正确的补救是回到 4.2 的旋钮，而不是往回加逐步奖励项。**
- **幅度项的固有噪声与轻度激励**：终局塔态有一部分是对手造成的，所以"顺利/艰难"混入了对手水平这个变量（打弱对手天然 margin 高）；且比赛胜负已定后继续拆塔能多拿分，会鼓励"追杀"而非收手。两者都因 β 被限制在 0.4 以内而无法改变胜负次序，属于可接受代价；若要更干净，可只在 `|margin|` 上取符号或对已判定的对局做截断，但那会牺牲"顺利程度"的信息量，暂不做。
- **引擎脆弱**：`.34` 曾死 4/4、`.35` 一台空转 21 核——看护必须支持单槽重启与冷启，否则 RL 的样本流会断。
- **动作空间巨大**（8 卡 × 576 格 × 61 延迟档）：联合探索困难 → 先按"card → cell → delay"条件采样 + 熵正则，并在 P2 单专精阶段先验证可学性。
- **样本需求 vs 引擎产量**：回合变长已使场/h 下降（§7），若产量不达标，先扩槽/扩机（`.39/.41` 还能各加 AVD），而不是加大模型。
- **两条线并存**：V4 联盟与事件模型 RL 应共享"快照池/评估剧本"的实现，避免两套度量口径。
