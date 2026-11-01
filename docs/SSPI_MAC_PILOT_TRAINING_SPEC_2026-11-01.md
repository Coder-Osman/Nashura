# Nashura：Mac 单机稀疏搜索训练验证规格

> 版本：`sspi-mac-pilot.v1.5` · 2026-11-02
> 定位：实施用训练设计，不是已实现、已跑通或已证明有效的训练结果。  
> 代码阅读基准：`Coder-Osman/Nashura main@5da75d6591fd5cf68c168d293a3b8d7078a2d96a`。  
> 唯一计算设备：用户的 Apple M4 Max / 36 GB 统一内存 Mac；真实原生游戏引擎**优先使用 macOS 上运行的 ARM64 Android Emulator（AVD）**，不是 VMware Ubuntu。不得假定远程 GPU、五机集群或额外服务器可用。  
> 本文取代此前 SSPI v1/v2 中的多架构、多参数量、历史 token、GRU、海量评测与集群部署建议；不修改历史实验事实。v1.4 按最新执行决定将训练固定为唯一 `5% × 128` 方案，移除 `40% × 128` 训练组。

## 1. 本次究竟验证什么

**核心问题：从随机权重开始，少量高预算搜索能否教出更强的无搜索策略？**

锁定一个网络、**固定的2.6速猪无英雄卡组**、一个原生引擎后端和一套动作时钟。唯一训练方案在每位玩家真实决策边界独立以 `p=0.05` 搜索，每个搜索根完成 `128` 次有效 PUCT 模拟；其余决策从冻结策略直接采样。主监督来自搜索根；v1.5 另外允许最终实际执行的搜索 PLAY 根派生中间观测，按第 7.5 节分配同根训练权重。不设置第二训练组；无搜索评估仍与随机初始化策略比较，以检查是否出现有效学习信号。

本轮不是论文级结论：一个初始化种子、先做小规模配对评测；不给跨种子泛化、最优架构、广泛卡组能力或强人类水平作结论。若暂时没有正信号，记录有效样本数、教师质量和学习曲线，按真实证据决定继续收集、排查错误还是停止；不为追求正结果临时引入新网络、奖励或预训练。**不设任何固定训练时长或总墙钟上限。**

### 1.1 已确定约束与本文固定默认

| 项目 | 规格 | 来源/性质 |
|---|---|---|
| 训练方式 | 随机初始化；唯一方案 5% × 128；搜索根及其已执行 PLAY 的派生观测学习；无 PPO/GAE | 用户最新决定 |
| 出牌延迟 | 200、400、600、800、1000ms，恰好五档 | 用户已确定 |
| 神经记忆 | 无 LSTM/GRU、原始历史序列、previous_action 编码 | 前文已收敛 |
| 外部状态 | 保留公开 Tracker 对对手手牌、轮转与圣水的当前估计及不确定性 | 用户已确定 |
| 动作覆盖 | 本轮只执行 WAIT/PLAY；ABILITY 为未来扩展预留模式，始终非法；支持2/3张手牌 | 用户最新范围调整 |
| 模型 | 复用现有约 10M 级语义/实体/空间骨干，仅固定一个规格 | 本文工程默认 |
| 等待 | WAIT 最长 1000ms，允许规定事件提前唤醒 | 本文固定默认；不另测 WAIT 消融 |
| 技能时序 | 本轮不执行技能；未来接入时复用已有按来源验证、下一 native tick 执行的无指向技能协议 | 仅预留接口，不做实际支持承诺 |
| 训练组 | 单一 `5% × 128` 配置；不运行第二训练组 | 用户最新决定 |
| 范围 | 现有固定2.6速猪卡组（HOG_DECK），同卡组自博弈；不换卡组 | 用户最新明确要求 |
| 运行规模 | 单 actor，分阶段采集/训练；以首批三代为观察里程碑，视真实结果继续；**无总时长或单臂时长上限** | 用户明确要求，不做硬性时间限制 |

“最终规格”意味着不再进行架构讨论和超参扫描，不意味着下面尚待本机检查的能力已经通过验收。

## 2. 源码审查后的最小改动

| 现有代码 | 本阶段怎么处理 |
|---|---|
| `fair_final/model.py` 已有无循环隐藏状态的语义/空间/关系骨干，但旧条件头仅 WAIT/ACT、31档延迟 | 复用骨干；新建版本化的三模式门控（WAIT/PLAY/ABILITY）和五档出牌头；Hog 对局屏蔽 ABILITY |
| `event_abilities.py` 已有按来源实体、技能 ID 和原生回执执行的接口 | **本轮不接入执行/搜索**，只作为未来 ABILITY 适配器的接口参考 |
| `fair_final/native.py` 当前只负责出牌预约，技能队列不是自动兼容的 | 本轮沿用纯出牌路径，绝不为了扩展性去重构原生技能执行器 |
| `fair_final/refill_belief.py` 表达物理空槽和延迟补牌，且固定 HOG_DECK | **这正好符合本轮速猪范围**；保留 Hog 专用不变量，但别把它们写进通用模型/动作 schema |
| `fair_final/learning.py` 的 WDL loss 会覆盖全部传入行 | 新 learner 在策略和价值两个 loss 之前只索引有效 SEARCH 行；DIRECT 永不反传 |

### 2.1 当前只用2.6速猪

双方固定使用仓库已有的 `HOG_DECK`、基础形态、固定等级与比赛模式，具体 card ID 和规则 SHA 记入 `run_config.json`。不查找、不替换、不训练英雄卡，不做包含英雄的 native 对局，不要求技能实际执行回执。模型的 `ability_candidates` 当前是空集合，`ABILITY` 在当前对局始终被掩码。

**扩展性边界**：保留动作的标签式类型、三模式门控、可变长度能力候选及序列化槽位，未来加入英雄只需为这些接口提供实际候选、原生执行适配和对应训练数据；不能声称零工作，但不需要重写关系/空间骨干或整个 MCTS/learner。当前没有英雄训练样本，能力头不会被训练。

这里的代码审查只证明组件存在，不代表已在这台 Mac 上完成128搜索或新训练闭环。

## 3. 当前决策状态：没有神经历史，不丢必要状态

网络输入是 `x_t = (当前战场, 己方当前状态, 对手当前信念摘要, 时间/规则, 合法性)`，不是 RGB 截图，也不是对局历史序列。

外部 Tracker 持续消费公开事件，把历史压缩为“现在知道什么”。原始日志可保留用于状态重建与审计，不送入网络。Tracker 确定知道的事实与估计必须分开；未知不等于零，不等于不在手中。

| 输入流 | 必须保留的内容 |
|---|---|
| 战场实体/群体/子单位 | 类型、所有权、位置、速度、血量/护盾、可见目标、攻击/部署阶段、相关冷却、当前可见效果；未知字段有 mask |
| 塔与地图 | 塔血、激活状态、当前攻击阶段；现有合法地图几何与实体空间投射 |
| 己方八卡身份 | 规则属性、`in_hand_ready`、槽位、轮转/可见下一张、已知可用性；牌身份不随手牌空槽重排 |
| 己方四个物理手牌槽 | `occupied`、实际 card ID 或 EMPTY、补牌是否待完成、可获知的剩余时间/顺序及 known mask |
| 己方资源 | 精确圣水、恢复阶段；当前有关的资源预约状态，不重复推断过去动作 |
| 可选技能候选 | 本轮恒为空列表；保留 `ability_candidates` 类型和 mask；未来可由来源实体、能力 ID、费用/冷却等合法状态构造，不将原生实体 ID 当连续数值特征 |
| 对手信息 | 已揭示卡牌、轮转/在手可用性的已知/未知状态、圣水区间；本轮不维护对手英雄技能信息 |
| 时间/规则 | 剩余时间、加时/倍率/模式、当前影响动作结果的规则阶段 |
| 动作掩码 | 同一根上的 WAIT/PLAY/ABILITY 模式掩码、卡牌/位置/延迟掩码；`ability_candidates=[]` 时 ABILITY 必须为 False |

禁止：真实对手未公开手牌/牌序、私有资源真值、对手尚未公开预约、真实比赛 RNG 状态、native audit digest、对手本次搜索结果。

尽量沿用既有 FAIR 张量定义，不为了“必要信息”直接开放原生内存字段。自己的补牌到达时刻等字段若只能从引擎私有实现读到且不属于约定 FAIR 输入，只能使用公开规则推断并标注 known，或保持未知；不能假称单帧可观察。

### 3.1 删除 previous_action，但保留未完成动作的真实影响

上一动作已经发生的结果由当前单位、资源和卡牌状态表示，不再单独编码“上次选了什么”。

本版在**己方预约已经执行、取消、被事件打断或到期后的决策边界**调用模型，故该玩家本人的未决预约应为空；已有 native 部署动画仍作为当前物理状态。若某适配器允许预约未结算就重新决策，必须先规范化为相同边界，不能删除预约信息后继续用不完整状态决策。

整个 AI 系统仍有 Tracker、计时器和快照状态；“无状态模型”不代表系统不需要状态恢复，也不保证这个紧凑摘要是数学上完全充分的 belief state。

## 4. 不足四张手牌与补牌延迟：本轮必须正确

### 4.1 三种东西不能混为一谈

- **实际就绪手牌**：native 当前手牌槽中确实存在、可以按规则提交的牌。
- **下一张/待补牌**：已知身份或顺序，但尚未进入可用手牌槽；不能出。
- **静态八卡身份集合**：卡组的身份目录，不表示八张牌当前都在轮转队列里。

例如 `hand_slots=[A, EMPTY, C, EMPTY]`，下一张 B 已知、随后 D 在排队：此时只有 A、C 可成为 PLAY 选择；B、D 可以影响决策和价值，但不应因“下一张可见”被当成在手牌。

### 4.2 最小数据表示

固定八个卡牌 token 表示卡身份；四槽描述表示物理占用。不把手牌强行补齐四张，不复制最近一张牌，不用零 ID 充当真实卡，不按紧凑数组下标替代 native handIndex。

每个槽至少有 `occupied, card_id_or_empty, refill_pending, refill_remaining_ticks, refill_time_known`。有多个待补牌时保留原生队列顺序/可知信息；必要时以最多四个小记录表示，不引入历史 Transformer。下一张牌提供 `card_id, identity_known, available_now=false`，实际到位后才更新 readiness。

**本版不支持“现在预选一张还未到手的牌，等它补出来自动打”。** 这不是新规则，只是本次控制器的简化动作边界：PLAY 根候选必须现在在手，允许等待五档中的合法时刻支付；补牌到达后唤醒并重新决策。不得把未来到手当作当前合法。

### 4.3 由原生引擎维护补牌，控制器不补牌

补牌周期、阶段转换和排队截止由真实引擎决定。旧材料中的20/10/7 tick是特定模式的历史观测，不能硬编码为所有模式的通用规律。计时特征必须使用当前绑定规则与已验证的事件定义，阶段切换是否追溯改变已排队补牌也以引擎为准。

己方手牌从2→3→4张时，外部状态、合法掩码、缓存和策略决策同步更新。等待期间即使没有敌方出牌，也必须在本方补牌到位时获得决策机会；不需要重新固定每帧推理。

### 4.4 Hog 专用轮转允许使用，不能泄漏进通用接口

本轮可沿用 `HOG_DECK`、八卡身份和真实 Hog 轮转的守恒检查，因为没有英雄。但 `CardState`/`HandSlot`/`Action` 对外合同必须表达空槽和待补入状态，而不是要求始终四张实际在手牌；`len(hand)+len(cycle)==8` 等 Hog 特例只留在本轮规则适配器。

未来若加入英雄，另替换其专用 `deck/hand/cycle` 适配器，网络仍消费同一类有 mask 的状态摘要。现在不实现英雄在场回转、特殊排队或技能冷却。**真实对局和搜索信念世界必须同步保留2/3张手牌的补牌时序**，禁止搜索世界擅自补齐四张。

## 5. 固定模型：复用约10M级骨干，不再比较架构

为减少实现成本，本版选择现有 V4/`fair_final` 路线的固定规格，而不是再实现前稿的192维双向融合新网络。参数量以用户现有约10M为参考，不增加大LSTM，也不为了凑到精确10,000,000而填充无用参数。

**固定规格如下；最终只统计一次实际参数量和各模块占比，不做5M/10M/20M扫描。** 预留小型技能分支可能改变精确参数量，但本轮无合法能力候选，技能分支参数不接受有意义的梯度；不声称模型恰好等于10M。

| 模块 | 固定规格 |
|---|---|
| 卡牌/机制语义 | 复用规则表和128维语义编码；所有可学习参数重新随机初始化 |
| 子单位编码 | 128维；群体内4个学习池化槽；保留精确子单位位置进入空间分支 |
| 实体关系网络 | `d_model=256, layers=4, heads=8, FFN=1024`，沿用相对几何/关系偏置 |
| 实体容量 | 己方8卡、对手≤9行（含未知汇总）、6塔、≤48群体、≤160子单位；溢出按现有显式规则处理 |
| 空间网络 | `[B,64,32,18]`；现有 scatter + 3个标准空间残差块，不做卷积种类/层数对照 |
| 标量/槽位状态 | 当前规则/资源/补牌/Tracker摘要编码为128维；四槽小特征先池化，不是历史编码 |
| 候选摘要 | 128维，实际在手牌和固定的空能力候选接口；不将搜索随机候选表当作状态特征 |
| 全局融合 | `scene256 + spatial128 + scalar128 + candidate128 = 640` →512；2个512维残差MLP + LayerNorm |
| 模式头 | 512→256→3：WAIT、PLAY、ABILITY |
| 卡牌头 | 全局query与己方8卡token匹配，按当前合法在手身份mask |
| 位置头 | 卡牌条件化query与576个空间key匹配，key宽128，按该牌位置mask |
| 出牌延迟头 | `[h512, card256, selected_cell128]`→256→5 |
| 预留技能来源头 | 极小的通用 `AbilityCandidate` query/key pointer 接口，可使用来源实体/技能语义/费用/冷却特征；本轮空候选且完全屏蔽，不要求真实执行器 |
| 价值头 | 512→256→3：WIN、DRAW、LOSS，`V=P(WIN)-P(LOSS)` |
| 删除 | EventSetEncoder、PreviousActionEncoder、任何持续RNN、历史tokens、额外辅助预测头 |

推理顺序：当前状态一次编码 → 三模式分布（Hog 必屏蔽 ABILITY）→ WAIT 或 PLAY 的条件选择。不要为每张卡或每个格点重跑CNN。空间key、卡语义和状态编码仅在冻结模型/相同输入/mask身份下缓存；权重更新、补牌、冷却变化或快照恢复后的新输入必须使不适用缓存失效。训练时禁止复用旧权重下的特征缓存。

输入字段宽度改变应使用独立版本配置，不原地破坏 `ModelConfigV4` 对历史schema的固定检查。

## 6. 动作协议：本轮只执行速猪，未来技能不用改主干

动作数据用稳定的**标签式联合类型**，而不将英雄技能硬编码成第九张牌：

```text
WAIT()
PLAY(card_id, cell, delay_index)          # delay_index ∈ [0,4]
ABILITY(source_entity_id, ability_id)    # 未来扩展保留，本轮不能选择或执行
```

模型模式头从一开始就是三 logits：`WAIT / PLAY / ABILITY`。Hog 对局的 `ability_candidates=[]`、`legal_mode[ABILITY]=False`；在 mode logits 上先掩码再归一化，ABILITY 概率恒为0且不能生成搜索候选、动作回执或训练目标。无合法在手牌时 `PLAY=False`，WAIT必须可选；不可对空候选做 softmax。

当前完整策略概率：

```text
P(WAIT) = P(mode=WAIT)
P(PLAY,c,xy,d) = P(mode=PLAY) * P(c|PLAY,x_t) * P(xy|PLAY,c,x_t) * P(d|PLAY,c,xy,x_t)
```

出牌延迟严格五档：`200,400,600,800,1000 ms`，各为`4,8,12,16,20`个50ms native tick；`WAIT`默认20 ticks，并接受已定义的公开事件/本方补牌完成唤醒。Hog 当前有 `1+8×576×5=23041` 个基础动作身份；今后 ABILITY 候选数量可变，**不要用23041写死全游戏动作空间大小**。推荐记录键为 `wait`、`play:<card-id>:<cell>:<delay-index>` 和预留 `ability:<source-id>:<ability-id>`。

未来打开 ABILITY 时，定义 `P(ABILITY,u,k)=P(mode=ABILITY)*P((u,k)|ABILITY,x_t)`；来源指针只遍历同根合法候选，技能不使用出牌位置和五档延迟。其来源ID仅用于路由，不当作连续可学习数值。届时为真实技能引擎实现下一tick的执行与回执验证，并补对应技能样本；这部分是**延后工作**，不属于当前Mac速猪通过标准。

为防止以后重写数据/损失协议，本轮就要求 `Action` schema 允许上述 tagged union、`AbilityCandidate` 可为空、候选特征与mask采用有版本的接口，MCTS访问、序列化、联合 `log_prob` 和learner按 action kind 分发；只需一个**合成能力候选的轻量单元测试**检验接口可扩展，不做英雄native测试。

同tick双方决策仍先冻结各自 FAIR 视图，再共同提交；搜索不能因未来能力占位访问当前不可见信息。

## 7. 稀疏搜索、自博弈和教师目标

### 7.1 一步的定义

“步”是某位玩家一次真实决策边界，不是物理帧、不只是成功出牌。补牌唤醒形成的新决策也进入同一门控（未来若有技能事件，也按相同定义）。双方各用独立门控RNG，以 `(run, arm, game, owner, decision_index)` 绑定；不按胜负、手牌数量或模型熵挑搜索点。

`p`是期望比例，不强行每局凑5%。即使一次根只有WAIT合法，也不悄悄改变门控统计；每个被选中的搜索根必须完成128次有效访问。

### 7.2 每代收集权重冻结

搜索决策使用 `SEARCH128`：用当前冻结网络提供先验与叶WDL，执行访问分布采样。DIRECT：完整联合策略采样，温度1。两类行为都真实执行并改变同一条对局轨迹。

同一局/同一代不热换网络。双方从同一代自博弈起步；不引入历史1755教师、专家开局、PPO轨迹或额外对手联赛。记录对局结果前不发布可训练样本。

### 7.3 固定搜索规格

| 参数 | 固定值 |
|---|---|
| 算法 | PUCT；`c_puct=1.5` |
| 搜索根预算 | 固定128；只计完成有效边并回传的模拟 |
| 候选上限 | 32，初始开放至少8或全部强制覆盖项 |
| 强制覆盖 | WAIT、每张当前合法在手牌至少一个出牌动作；ABILITY当前没有合法候选 |
| 其他候选 | 沿用位置峰、延迟邻居、完整联合概率采样；去重；不把真实隐藏状态用于提案 |
| 探索 | 根非WAIT分支噪声比例0.25、Dirichlet总浓度10；固定单组规则；保留原始与搜索先验 |
| 行为/标签 | 温度1；`pi_search(a)=N(a)/sum_b N(b)`，不锐化标签 |
| 深度/时域 | 复用completed-boundary约束：软6个同时提交边界组/200ticks，必要时最多再6组/60ticks完成当前边；未完成边不回传 |
| 信念 | 每搜索根一个独立兼容世界；本次不做1/4/8世界扫描 |
| 模拟中的对手 | 冻结策略基于其合法信息采样；不递归嵌套完整MCTS |
| 下一实际根 | 新建搜索统计；不做跨根复杂树复用 |

本轮候选只含WAIT与合法在手牌PLAY，32不是穷尽23041个基本身份。未来启用ABILITY时在同一个候选生成接口添加合法技能来源的提案与覆盖检查，并在必要时调整候选容量；不能把技能当作位置头里的伪卡牌。

单信念世界是本次节省工程量的近似，可能过拟合错误隐藏状态；本文不声称解决了信息集搜索或求得Nash策略。禁止为简化世界构造复制真实对手隐藏手牌。Hog轮转、补牌空槽、圣水与公开Tracker状态必须在兼容世界中合法解释；未做到则不能进入主训练。

### 7.4 只使用搜索根的两个loss

自然终局后，对每个验收搜索根按根玩家视角回填 `z in {WIN,DRAW,LOSS}`：

```text
L_policy = mean_roots[- sum_a (N_root(a)/N_budget) * log pi_theta(a | x_root)]
L_value  = mean_roots[- log WDL_theta(z_root | x_root)]
L        = L_policy + L_value
```

网络概率在**完整合法条件动作空间**归一化，不只在32个搜索候选上归一化。本轮PLAY包含卡、格点、五档延迟，WAIT仅包含模式；预留ABILITY未来仅包含模式与技能来源指针，本轮训练权重为零。

**DIRECT行既不进入策略loss，也不进入价值loss、辅助loss或PPO。** 失败、截断、预算不足的根不能贴成完整标签；超时不是平局。本次为简化失败处理，搜索预算不完整或协议错误就中止并隔离整局，不自动用DIRECT补缺后继续发布训练样本。所有成功完成且自然终局的对局（包括输和平）按相同规则接受，另外报告隔离率，防止幸存者偏差被隐藏。

### 7.5 已执行 PLAY 的中间观测（2026-11-02 用户确认）

在真实对局已经经过的 tick，每 200 ms 复用一次 FAIR 观测，不触发新决策、推理、搜索或游戏推进。仅为 SEARCH 根提交的 PLAY 采样；到原 reservation 截止时实际 executed 才保留，preempted/rejected/terminal_expired 一律丢弃。200/400/600/800/1000 ms 原延迟分别最多派生 0/1/2/3/4 个点，执行时刻本身排除。

派生点保持卡牌和格点，动作延迟改为剩余延迟，并重新检查当时的完整合法空间。原根仍使用完整搜索访问分布；派生点使用单动作行为克隆标签，不冒充新的搜索统计。自然终局后按同一 owner 回填 WDL，不新增模型头。独立 tensorizer 的 Tracker/可变状态与缓存隔离，不影响实际 actor 的后续输入。

有 k>0 个有效派生点时，`L_group = 0.5 * L_search_root + (0.5/k) * sum_i L_continuation_i`，其中每个 L 包含 policy CE 和 WDL CE；没有派生点时原根权重为 1。所有组取平均，派生点附着原根，共享 replay 分区及两次曝光额度，不计为额外 SEARCH 根。默认启用，collector 的 `--no-temporal-augmentation` 或运行配置 `temporal_augmentation=false` 保留原基线。

本节是 v1.5 对原 SEARCH-only 主监督的显式增补，DIRECT 行仍不参与训练。旧 episode 没有派生点时保持可读；有派生点的数据校验源根、reservation、实际执行回执、动作/剩余延迟、FAIR 摘要与终局视角。实现与工程测试见 [中间观测增密记录](SSPI_TEMPORAL_AUGMENTATION_2026-11-02.md)，不代表原生完整训练或模型强度验收。

## 8. 优化、回放和随机冷启动

固定 `AdamW(lr=1e-4, weight_decay=1e-4)`；梯度裁剪1；有效batch128，默认microbatch4、累计32次。微批可因内存调整，累计维持同一有效batch；这不是架构实验。

所有可学习权重随机初始化、优化器/replay全新，规则表与静态语义不属于预训练。WDL末层用小随机权重、零bias，初始接近中性但不伪装为校准胜率。LayerNorm/现有规范化保留，新增dropout、奖励塑形、价值热身和辅助头本轮不启用。

终局价值按胜负平学习，不按“每个决策”折扣，避免更频繁技能/补牌唤醒改变同一物理时间的奖励定义。

本轮最多保留三代搜索根，CPU/磁盘存储，按完整对局分组。每代新有效训练根最多提供两次样本曝光额度；**每条根整个本轮最多被训练两次**，旧根不因进入新一代自动重置曝光。优先均衡选择尚有额度的对局，局内无放回轮转；不足一个有效batch的余额结转，不无限复用小集合凑更新。

train/validation/test以整局及配对种子组划分，双方同局不跨分区。无需扫描所有历史实验的全部seed；至少保证初始诊断、单方案采集、验证及最终评测按清单隔离，重跑不重复计入数据。

随机WDL可能让128次模拟仍稳定放大错误估值，全部draw也可能令学习停滞。记录自然胜/平/负、根Q/访问熵、终局叶比例和实际前瞻游戏时长。没有学习信号时诚实归类为冷启动/标签质量瓶颈，不临时加载旧模型或把95%直出步加入训练。

## 9. Mac 单机 Android Emulator 部署与成本边界

### 9.1 运行拓扑：Mac 宿主 + 本机 ARM64 Android Emulator（**不使用 Ubuntu VMware**）

```text
macOS / Apple Silicon M4 Max / 36 GB Unified Memory
  ├─ Python 控制器、冻结 Policy/Value 推理、训练器（CPU 或 MPS，按实测）
  └─ adb forward / localhost TCP 接口
        ↓
本机 Android Emulator：arm64-v8a AVD（既有 astral-api30 优先）
  ├─ Engine 0：真实完整对局
  ├─ Engine 1：玩家0独立信念世界
  └─ Engine 2：玩家1独立信念世界
```

**硬约束**：不启动或 SSH 连接 VMware Fusion 的 Ubuntu/Linux guest，不使用 `vmrun`、`ssh` 到该 guest、Linux/Bionic headless 运行包或 QEMU x86 ARM 翻译链路作为本次训练入口。它们属于此前**独立的脱离 Android 研究主线**，与本次 Mac AVD Pilot 隔离。已有 Linux guest/实例不要关闭、清理、重建或改动。若 AVD 路径经实测不可用，仅记录具体阻塞，只有用户明确批准才能改用 VMware 或其他后端。

**不是从零部署**：仓库已经有 [M4 Max 本机 ARM64 AVD 实测手册](../firstlight_CR/docs/local_mac_capture_training.md)，记录了 `~/Library/Android/sdk`、`system-images;android-30;google_apis;arm64-v8a`、`astral-api30`、`adb`、原生 probe 的 ARM64 TBI tag 兼容与 `configure_emulator_engine_guests.py`。这份手册是**历史已运行记录**，本机实际 AVD、APK、资源和进程状态先检查，不能假设今天全部仍就绪。

最小只读预检（确认可用工具真实位置后执行，勿修改原有AVD数据）：

```bash
uname -m                             # 应为 arm64
ls "$HOME/Library/Android/sdk/emulator/emulator"
"$HOME/Library/Android/sdk/emulator/emulator" -list-avds
"$HOME/Library/Android/sdk/platform-tools/adb" devices -l
# 已启动目标 AVD 才查询；示例 serial 按实际输出替换
adb -s emulator-5554 shell getprop ro.product.cpu.abi
adb -s emulator-5554 shell getprop sys.boot_completed
```

使用 Android ARM64 镜像，macOS Hypervisor.framework 加速，不走 x86/ARM 指令转换；必要时 AVD 可使用 `-no-window -no-audio`（仅隐藏窗口/声音，仍是完整 Android 环境），先验证原生 Engine/FAIR/128根在该模式下可运行。不把 `-no-window` 冒充省略 Android 系统层或真实无显示战斗引擎。

优先复用既有 AVD 和引擎的 localhost TCP 接口。历史文档使用 `adb forward` 将 guest 引擎端口映射到宿主 `26789...`，原生客户端现有 `NativeClashEnv(host,port)` 可参考。若复用 `configure_emulator_engine_guests.py`，注意它检查 `nproc==--engines`、会 `am force-stop` 并重启 App：**先检查现存会话，不能直接用 `--engines 3` 去重配历史6-vCPU AVD，更不能打断其他实验**。只从当前已验收、独占的会话中分配实际比赛与两方信念世界，不追加无意义并发。

训练和数据采集分阶段执行：训练阶段停止自博弈采集，推理仍使用冻结 checkpoint。macOS 不直接 `dlopen` Android ELF `libg.so`；它运行在 Android ARM64 AVD 内。不得因此再套一层 Ubuntu VM。

### 9.2 内存与推理后端：CPU/MPS **按真实基准选择**

36GB 是 macOS、Android AVD、Python/模型、缓存和快照共享的统一内存，**不是**单独的36GB GPU显存。目标工作集先控制在约24GiB内并保留系统余量；出现持续 swap 或内存压力警告就暂停启动新局并保存 checkpoint。

首次只做一次小型正确性与吞吐对比：相同随机权重、真实 FAIR 张量、scatter/attention/五档动作掩码、前向和反向，分别在 `cpu` 与 `mps` 上测试兼容性、loss/梯度有限性以及中位耗时。先以 CPU 跑通最小闭环；如果 MPS 通过数值检查且更快，再切 MPS。记录实测选型、设备/torch 版本，不要因为硬件有 GPU 就假设 MPS 比 CPU 快，也不静默开 CPU fallback。

历史 [Mac训练实测手册](../firstlight_CR/docs/local_mac_capture_training.md) 在**另一种事件预训练负载**中记录 6 batch CPU 24.2s、MPS 57.9s；这是该负载的历史数据，不能直接推断新网络也慢2.4倍。FP32先跑通，不做量化、混合精度、CUDA Graph 或 `torch.compile` 的额外专项。[S8][S9][S10]

单个128根仍需实测成本。先测真实 AVD 根：常规根、2张手牌、3张手牌；记录完整根耗时、有效模拟、native tick、host/guest 请求往返、推理时间和峰值内存。优先优化真实热路径，不重新搭建Linux headless后台。

### 9.3 无硬性时长上限：按证据与资源状态推进

**本 Pilot 不限制总训练时长。** 删除此前「总8小时、分组各3小时、检查和评测2小时」的配额。训练可以持续进行多轮数据采集和参数更新；由实际学习曲线、搜索根耗时、原生引擎吞吐、样本多样性及机器资源状态决定是否继续。无需事前承诺多少小时完成，也不能为了达到或避开时限而更改每根128次有效模拟次数。

为了避免无限重复小样本，在每个完整代际节点记录：独立完整局数、真实 SEARCH 根数、有效模拟和原生 tick、每根耗时分布、训练/留出验证损失、无搜索评测得分、样本曝光以及CPU/MPS/内存/Swap状态。**持续训练不等于无限重用旧数据**；既定「每根最多两次梯度曝光」仍生效，不足样本时收集新局而非重复拟合。性能优化针对真实引擎推进、捕获、复原及必要推理热路径。

初期从少量完整局、单根真实128搜索和一代可恢复更新开始，再以三代作为首次观察里程碑；这不是自动停训上限。若训练表现仍在改善且资源状态稳定，可按相同协议继续增加完整代际/对局。若遇协议错误、不可恢复的 native 故障、持续内存压力、严重换页或手动停止，则在完整边界保存检查点、训练记录和阻塞证据；截断对局保留为无效 attempt。若学习信号不足，可先检查教师与数据质量，不以时间到点推断训练失败。

可以估算实际工作量供调度参考，例如 `T_game ≈ T_direct_game + p*D*T_search + tracker/belief额外成本`（D为双方真实决策总数），但这是性能诊断，不是启动下一场的倒计时或实验有效性门槛。报告实际墙钟、模拟次数与有效样本；无搜索评估对照只用于检验训练后策略相对随机初始化策略的变化，不构成第二训练组。


## 10. 最小检查：只验证真正会改变实验结论的路径

不重新做五机预检、全英雄矩阵或多平台性能竞赛。**先只读确认当前 Mac ARM64 AVD 与本地引擎 endpoint，不接触 Ubuntu VM**；受影响代码做小范围单元测试，加真实原生Hog对局关键回归；fixture不能代替真实完整终局。

| 必要检查 | 最小通过条件 |
|---|---|
| 本机引擎接入 | macOS ARM64 Android Emulator 中真实游戏启动，adb/localhost 端口合法连接、原生 FAIR 观测与状态快照可用；不依赖 VMware Ubuntu |
| 动作与掩码 | WAIT/PLAY五档 tick正确；三模式归一化；Hog能力候选为空、ABILITY概率和被采样次数均为0；无牌只WAIT；完整条件概率合法 |
| 未来扩展接口（仅合成） | 用虚构合法技能候选验证 action codec、mask、来源指针log_prob和搜索访问记录的类型语义；**不执行真实英雄，也不作为训练样本** |
| 手牌/补牌 | 原生Hog状态真的有3/2张时正常决策、多个空槽FIFO补齐；下一张未到不出；原生手牌位置与card identity对应；补牌唤醒不漏 |
| 快照A/B/A | 普通根和补牌关键根恢复native+Tracker+轮转/空槽+队列+RNG；同根同动作可重放，不能读真实对手隐藏状态 |
| 搜索 | 至少一次真实完整128模拟Hog根；模拟合法、搜索访问数严格等于预算、句柄清理完整 |
| 混合数据与梯度 | 至少一局真实自然终局，含SEARCH与DIRECT；只有SEARCH参与策略和价值两个loss；根玩家WDL方向正确 |
| 参数更新与恢复 | 一次实际梯度更新，读回checkpoint、优化器、RNG及曝光计数；MPS按记录容差验证，不声称跨CPU/MPS逐位相同 |

定向补牌状态可用脚本触发做诊断，但不得混入随机门控训练数据。**英雄技能真实执行、英雄冷却、在场特殊轮转以及任意含英雄卡组不属于本轮验收门。**

关键错误只能修复不能靠降验收、填满手牌或偷读私有状态绕过。

## 11. 最小有效性实验，不再做多模型矩阵

### 11.1 唯一训练方案

| 网络/初始参数 | 搜索概率p | 每根N | 训练组 |
|---|---:|---:|---|
| 本文固定网络，随机初始化seed `20261101`，独立新AdamW | 0.05 | 128 | 唯一方案 |

不设置第二个训练组，不复刻 `40% × 128` 对照。固定卡组、规则、Tracker、动作时钟、候选生成、噪声、深度、loss与每根曝光上限；每代采集时权重冻结，新权重只在下一代启用。

不做不同模型大小/层数/历史输入/多信念粒子对照，不用大规模预训练，不采用多代晋级淘汰或自适应调参。

### 11.2 有界运行流程

1. 一次性完成第10节检查，包括一局真实完整混合轨迹；诊断数据单独归档。
2. 保存随机初始化theta0。单方案先以三代为观察里程碑，每代初始目标16局训练+4局验证；这不是训练时长、代数或局数的硬上限。按完整局预分配，验证行永不用于更新。
3. 收集一代后，按新根数量和两次曝光额度进行有限更新；不硬凑固定几百个optimizer step。验证只用于监控，按既定周期保存每个完整代的checkpoint；独立评测使用事先选定的完整代节点，不能反复挑最低验证CE或最有利的一次对战结果。
4. 主方案跑不出有效样本或完全不能更新时，先定位功能问题；修复后按相同协议继续，不设人工截止时间。
5. 在预先记录的完整代际评测节点，用关闭MCTS的冻结策略与theta0对战。初次评测使用32个独立种子、每个种子换边两局，共64局；后续若继续训练，按预先记录的新代际节点评测，避免事后只挑最优结果。theta0仅为学习有效性基线，不作为第二训练组。
6. 评测统一完整联合策略采样、温度1、固定独立行为RNG、无根噪声；不要一方逐级贪心一方采样。若今后部署要用greedy，可另行研究；本轮不再增加解码实验。

上述局数和三代里程碑都是**初始观察规模，而不是总时间、总代数或总局数的上限**。报告实际墙钟、模拟次数、有效样本和配对评测得分；不据此声称不同训练框架之间的相对优越性。

### 11.3 最终只回答三件事

- **工程有效**：真实完整Hog局、2/3张手牌补牌正确、128有效根、搜索行梯度及断点恢复形成闭环；ABILITY保持不可选。
- **有学习信号**：训练后无搜索策略对theta0得分提高，不仅是CE降低。

得分率 `(win+0.5*draw)/games`；以完整换边种子对为重采样单元给出配对bootstrap 95%区间，种子数在评测前固定。区间覆盖50%就写“本次样本不足以区分”；64局和单初始化seed即使有优势，也只是pilot信号，不是广泛有效性证明。

若未胜过theta0但工程闭环正确：当前样本尚未支持策略改善；检查随机价值、教师质量与样本数，不立即归咎10M容量。没有完成足够自然终局：只给工程/预算结果，不给学习失败结论。

## 12. 足够用的产物与实施顺序

复用现有模块，只新增一个小型 `training/sspi_mac/` 实验包；不要照前稿把所有组件拆成十几个新框架文件。可按 `config/model/actions/collector/learner/evaluate` 六个职责组织；能稳定复用的底层接口直接引用，改协议的部分有独立版本。

建议实施顺序：**复核仓库已有Mac AVD链路 → 动作/补牌适配 → 快照和128根搜索 → 固定模型与只搜根loss → 单局闭环 → 单方案训练与无搜索评估**。不要先做大dashboard、分布式服务、自动调参或长期任务系统。

只要求这些产物：

```text
runs/<run-id>/
  run_config.json          # 源代码、规则、动作、模型、卡组、seed、设备与预算
  initialization.pt       # 全新theta0，不提交公开仓库
  episodes/               # 完整局结果、轻量动作账本、搜索根输入/访问/终局标签
  checkpoints/            # 每代末模型、优化器、RNG、replay游标/曝光账本
  metrics.jsonl           # 代际、实际成本、搜索/技能/补牌/学习指标
  evaluation.json         # 固定配对评测全部胜平负与区间
  report.md               # 结果或明确的未完成/失败边界
```

日志指标控制为：有效局/搜索根、门控实际比例、完整模拟数、自然胜平负、PLAY/WAIT次数（ABILITY应恒为0）、2/3张手牌出现及补牌次数、合法规则取消/协议错误、policy/value验证损失、根耗时/原生tick/推理时间、内存与样本曝光。异常根保留详细现场；不为每个正常native tick写巨量JSON或重复全历史哈希。

checkpoint原子保存后读回；收集权重固定，恢复验证模型/动作/规则版本。正常stop于完整局或optimizer step边界。拒绝旧10档/31档/纯出牌schema混入，不擅自覆盖历史部署和实验目录。

## 13. 本次刻意不做的工作

不测5M/10M/20M，不比较192/256维，不训练LSTM，不加历史tokens，**不放英雄卡、不执行英雄技能、不做英雄规则适配**，不换固定速猪卡组，不改战斗逻辑，不重新追求零Binder，不做集群/并发扩展，不要求400+2000局晋级，不做多seed正式统计，不引入价值预训练/辅助奖励/专家数据，不建立复杂自动审批式验收流水线。

保留正确性的底线包括真实Hog出牌、补牌空槽、FAIR信息、快照恢复、128有效预算、搜索行训练以及完整终局。任一项错误都会改变实验回答的问题。

## 14. 来源与交付边界

**用户要求是本规格的目标约束；下面代码是复用依据，不是本机完成证明。**

- [S1] [现有条件策略/WDL](../firstlight_CR/native_runner/training/fair_final/model.py)、[V4固定维度](../firstlight_CR/native_runner/training/v4/config.py)、[语义/空间组件](../firstlight_CR/native_runner/training/v4/components.py)。
- [S2] [技能动作说明与验收边界](../firstlight_CR/docs/event-ability-actions.md)、[精确技能来源与执行](../firstlight_CR/native_runner/training/event_abilities.py)。
- [S3] [Final原生执行与唤醒](../firstlight_CR/native_runner/training/fair_final/native.py)、[预约时钟](../firstlight_CR/native_runner/training/fair_final/timing.py)。
- [S4] [原生手牌空槽与逻辑补牌](../firstlight_CR/native_runner/training/fair_final/refill_belief.py)、[公开Tracker](../firstlight_CR/native_runner/training/fair_final/information.py)、[资源/轮转推断](../firstlight_CR/native_runner/training/tracking.py)。
- [S5] [搜索与有效边](../firstlight_CR/native_runner/training/fair_final/search.py)、[候选提案](../firstlight_CR/native_runner/training/fair_final/candidates.py)、[搜索世界](../firstlight_CR/native_runner/training/fair_final/search_runtime.py)。
- [S6] [旧loss](../firstlight_CR/native_runner/training/fair_final/learning.py)、[旧数据合同](../firstlight_CR/native_runner/training/fair_final/contracts.py)。
- [S7] 用户提供的 `SEARCH_TRAINING_FRAMEWORK_OVERVIEW_2026-10-31.md`、前稿 `Nashura_SSPI_Design_2026-11-01.md` 与 `Nashura_SSPI_v2_Network_Architecture_2026-11-01.md`。前稿为设计而非已实现事实；本文件明确锁定Hog-only训练、无神经记忆、保留未来技能协议和Mac单机预算。
- [S8] [PyTorch MPS后端说明](https://docs.pytorch.org/docs/stable/notes/mps.html)、[MPS可用性API](https://docs.pytorch.org/docs/stable/backends.html)。本机采用已验证可工作的PyTorch版本，不为“最新”强制升级。
- [S9] [PyTorch MPS环境变量](https://docs.pytorch.org/docs/stable/mps_environment_variables.html)，含CPU fallback与内存水位限制说明。
- [S10] [仓库已有 M4 Max 本机 Android ARM64 AVD/采集与训练实测指南](../firstlight_CR/docs/local_mac_capture_training.md)；[现成的 AVD引擎配置入口](../firstlight_CR/native_runner/training/v4/configure_emulator_engine_guests.py)。
- [S11] [Android 官方 Apple Silicon/ARM64 VM加速](https://developer.android.com/studio/run/emulator-acceleration) 与 [模拟器命令行 no-window](https://developer.android.com/studio/run/emulator-commandline)。

v1.4 将训练预算定为唯一 `5% × 128`，移除训练组间对照；theta0只保留为关闭MCTS的学习有效性评估基线。代码已同步收敛预算注册表和训练探针入口；`astral-api30`、设备连接与现有本机端口已只读核验在线。当前仍待完成新预算的真实完整搜索/对局、参数更新、checkpoint恢复和无搜索评估；此前旧预算中断attempt不纳入训练。
