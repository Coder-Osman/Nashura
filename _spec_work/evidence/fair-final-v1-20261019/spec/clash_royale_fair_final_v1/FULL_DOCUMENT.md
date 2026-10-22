# 皇室战争 FAIR AI — 最终完整训练设计技术文档 v1.0

**2026-10-19 · Design Freeze Candidate**  
**状态：仅文档交付；没有修改代码、没有启动训练，没有对A checkpoint/五机现况作实际验证。**  
**规范优先级**：用户Q1–Q43的最后确认 > 本文；遇到引擎事实冲突必须暂停实施并报告，不得擅自修改已锁定决策。  
**两个不可更改的约束**：正式搜索仅单信念世界；Champion必须是**同一权重checkpoint**同时通过128次搜索与条件自回归随机直出两项晋级。

## 全文导航

1. A. 完整技术协议 —— `TRAINING_SPEC.md`
2. B. Q1–Q43 决策登记 —— `DECISION_REGISTER.md`
3. C. 数据契约 —— `DATA_CONTRACTS.md`
4. D. 实验矩阵 —— `EXPERIMENT_MATRIX.md`
5. E. 分阶段验收门槛 —— `ACCEPTANCE_GATES.md`
6. F. 实施顺序与交付计划 —— `IMPLEMENTATION_PLAN.md`
7. G. 风险登记 —— `RISK_REGISTER.md`
8. H. 参考资料 —— `REFERENCES.md`

---


---

# A. 完整技术协议

# 皇室战争 FAIR AI — 完整训练技术协议 v1.0

**版本**：2026-10-19 / Design Freeze Candidate  
**用途**：实施前的规范性设计，不是执行记录。**没有核验现有源码、源 checkpoint、五机硬件状态；没有修改训练代码或启动训练。**  
**决策来源**：用户原始《从A迁移：FAIR AlphaZero与完整联合动作训练》与会话Q1–Q43的最后裁决。参见 [`DECISION_REGISTER.md`](DECISION_REGISTER.md)。

## 0. 目标、优先级与规范术语

**优化目标**：在遵守 FAIR 信息边界、真实游戏规则及同 tick 并发时序的前提下，以五机总计算预算换取长期可复现的更强实战表现。两个交付指标分别是：同一网络 checkpoint 的128次搜索控制器，与同一网络 checkpoint 的无搜索自回归随机直出控制器。两者均须单独过关才能发布冠军（Champion）。

- **LOCKED**：用户明确批准，实施不得擅改。
- **BASELINE**：必须可复现的对照配置，其数值不等于已证明最优。
- **ABLATION**：已允许实验的候选机制；未通过实验不能宣称更强。
- **VERIFY**：引擎/权重/硬件的实际事实，必须只读核对。
- **BLOCKER**：未通过禁止进入下阶段或五机放量的门槛。

优先级：**规则/FAIR正确性 > 正确动作概率与决策边界 > A迁移 > 搜索确实更强 > 搜索标签能被学到 > Champion实战 > 单位算力效率 > 资源利用率。**

### 0.1 三种策略与三个版本角色

定义采集行为 `mu_behavior`、策略监督 `pi_train`、部署策略 `pi_deploy`。它们可来自同一个权重但**不保证是同一个概率分布**。定义持续更新的 **Learner**、有条件参与采集的 **Actor anchor / Candidate** 与双项通过才发布的 **Champion**。Learner 的权重不能自动视为 Champion。

`checkpoint_id` 是 **权重 SHA + 网络/动作/编码器/预处理/FAIR协议版本**；`controller_id` 进一步包含搜索协议和解码温度。官方要求同一 checkpoint，不表示搜索与直出必须使用同一解码机制。

## 1. 环境时序与动作契约（LOCKED）

### 1.1 动作类型

仅有 `WAIT` 和 `ACT(card, x, y, delay_ms)`。不引入 `CANCEL`。原固定词表具有 `1+8×18×32×31=142,849` 个槽位；0ms是保留编码但不可执行，实际延迟仅为 `100,200,...,3000ms` 共30档。空间保持18×32，不能未经消融削减网格或时间精度。这里8表示原编码卡牌身份槽数；当前能操作的手牌数以真实引擎为准（VERIFY）。

两个动作都是**可被后续决策抢占的预约**，不是不可撤销的最终命令：

1. 在某次合法信息状态下，玩家提交WAIT或ACT指令；若指令需要等待，将其作为唯一待执行指令（pending）记录。
2. 如果在执行到期**之前**触发新的合法决策，则旧pending立即作废，无专门取消动作；新决策产生新的WAIT或ACT。
3. 如果预约到期与新事件在**同一tick**，按用户Q16约束，**到期执行结算先于该tick产生的新决策**。双方该tick决策必须先分别冻结可见信息，分别生成动作，再原子提交，不能让一方读到另一方尚未公开的同tick选择。
4. ACT实际到期时重新检查合法性（Q17）。如果不合法，按经源码核验的环境拒绝/空操作及后续唤醒规则处理；不能在训练与搜索中私自选择不同语义。
5. WAIT最大游戏时间唤醒间隔3秒；提前唤醒只能来自对该玩家FAIR合法的触发事件。
6. 离线对局中，**模型思考和MCTS耗费的墙钟时间不推进游戏时间**。100–3000ms表示模拟器内时间，不含CPU/GPU推理时间；资源调度仍以真实墙钟成本计费。

必须写出可执行的引擎阶段时序表，覆盖“到期执行、同tick公开事件、观察冻结、双边动作提交、预约替换、拒绝、次级唤醒”。若现有引擎与已锁定语义矛盾，**停止并请求澄清**，不能默默改变引擎规则或规格。

### 1.2 决策边界（尤其重要）

`DecisionBoundary` 为控制器具有FAIR合法观察、可提交新WAIT/ACT，且所有**前一决策预约的执行/抢占/失效判定均已完成**的状态。它不是任意100ms状态，也不要求场上先前召唤的单位战斗结束。

Value网络只在DecisionBoundary上评价。只在真实DecisionBoundary处采集Policy/Value数据，**不采集中间pending状态训练样本**。模拟在截断前如果进入未完成预约的转移，须回退至上一个合法完整边界，不得给未完成边伪造价值或访问；详见§5.5。

## 2. FAIR信息状态与单世界补全（LOCKED）

对玩家i，采用信息状态 `I_i=(公开历史, 己方私有可见信息, 己方控制状态)`。网络、候选、节点索引、时钟与唤醒事件均严格只用其FAIR可见信息。真实模拟状态可用于游戏推进/事后审计，但不能给根控制器“真牌序”或未公开计划。

- 每个MCTS根只抽取**一个**隐藏状态 `h* ~ b(h|I)`，在这一根的128次模拟中保持这一补全世界；不做4/8粒子正式搜索。
- 先根据公开牌、历史循环与规则生成合法兼容集合，再用版本化、可复现实验的经验卡组先验加权抽样；不能从真实对手私有状态偷取真值。
- 若公开信息已足以唯一重建牌组/手牌/牌序，可将相应部分确定化；这不意味着对手未来动作、未公开计划或未公开随机性已知。
- 对手模拟看到**它在假想世界中依法应知的信息**，且禁止读取该世界中属于己方私密的信息。
- 搜索己方节点只以自身FAIR历史做身份；模拟转移、价值返回受 `h*` 影响仍有单世界偏差。此方法不声称最优、不声称策略融合风险已被消除。
- 冻结的模拟对手从Champion及代表性历史对手池选择，具体每局或每根对手ID固定并记录；对手每次决策仅按冻结自回归策略采样，不执行自己的MCTS。
- 真正对局的对手若执行搜索，模拟对手却直出，这是明确的**opponent model mismatch**，应单独分组记录并在搜索门槛中检验。

**FAIR非干扰门槛**：给两份FAIR信息完全一致、真实隐藏值不同的根状态，在固定的信念采样随机流和冻结模型下，根动作分布/实际采样结果应一致；隐藏真值不得影响候选、合法mask、唤醒、缓存或超时分支。事后可用真实隐藏状态估计模型偏差，但不得反馈在线搜索。

## 3. 网络：共享编码器 + 精确自回归联合策略 + WDL（LOCKED）

网络 `f_θ(I)=(p_θ(.|I),WDL_θ(I))`：保留A中严格兼容的语义编码器和buffers；新/可迁移的全局512维特征及两个512维残差MLP块作为基线，输出两个逻辑Head。不存在持久跨决策神经隐藏状态。若原A与新编码器实际尺寸/含义不兼容，不按形状碰运气复制。

### 3.1 条件联合分布（不是独立头）

对完整动作：

`pθ(WAIT|I) = pθ(mode=WAIT|I)`

`pθ(ACT,c,l,d|I) = pθ(ACT|I) * pθ(c|I,ACT) * pθ(l|I,ACT,c) * pθ(d|I,ACT,c,l)`。

完整合法ACT的 `log_prob` 是四个条件log-prob之和。Card/Position/Delay的网络实现可使用条件embedding、空间热图与30档时间Head；禁止把三个条件因子替换成无条件独立乘积。采用前缀可行性掩码：每个采样中的合法前缀，必须至少存在一个合法后缀；未来圣水可支付须按引擎合法时间窗口判断，不可用“当前不可支付”错误剪掉延迟可支付的牌。

**非常重要的区分**：逐级mask再条件归一化会定义一个自洽的新分布，但一般**不等价于**对旧扁平联合概率在所有合法动作上全局截断后再归一化。若需要两者相等，必须计算各前缀的剩余合法概率质量（backward partition masses）。迁移验收应分别测试这两种定义，**明确选定并版本化适配器语义**。

### 3.2 温度与直出

正式无搜索部署采取条件联合分布的随机采样。Mode/Card/Position/Delay可以在独立验证集上分别校准温度 `(T_m,T_c,T_l,T_d)`，`(1,1,1,1)`为基线。不同温度定义新的完整联合分布，训练原始 `log_prob` 和部署带温度 `sample` 不得混淆。所有温度、随机流、非法动作处理与checkpoint一起冻结为版本化 `direct_controller_id`，最终确认测试不能调温。

### 3.3 价值函数

`WDLθ(I)=softmax(WDL_logits(I))`，奖励向量按根玩家视角为 win=+1, draw=0, loss=-1，搜索标量 `vθ=P(win)-P(loss)`。树内根玩家视角固定；**不能因经过一次事件/对手动作就机械反转符号**，仅在跨玩家视角表示时显式换算。

## 4. 迁移A：保留现有能力而非复制旧错误（LOCKED/BASELINE）

### 4.1 来源与只读审计

仅接受经重新检查来源/导出身份后确认的A。原计划提供的 checkpoint SHA256：

`e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015`

其声称与旧学生第128步参数一致，这仍是VERIFY，不是本轮已验证事实。A完整冻结于 `eval()`，禁止更新参数和buffers；记录训练脚本版本、导出版本、catalog映射、特征顺序、棋盘坐标系、原延迟61档的确切语义与FAIR过滤契约。

### 4.2 ASourceAdapter

- 维护100–3000ms有效动作与旧61档的严格映射；0ms与>3000ms一律非法；WAIT最大3秒，与提前唤醒规则一致。
- 原A的逐级贪心作为**行为忠实度诊断**；旧模型在新的合法mask下可能改变模式/card分布，不能声称与旧世界完全等价。
- 对兼容语义/名字/shape的旧Encoder或自回归条件头拷贝参数和buffers；新512主干或不兼容头保留新初始化，用A条件分布蒸馏。不能把旧WDL头硬接到语义不同的512维主干。
- 蒸馏A对合法条件前缀的完整分布（包括WAIT/ACT、Card、Position、Delay），另保留完整贪心动作NLL辅助目标。价值蒸馏采用 `KL(WDL_A || WDL_θ)`；如采用全合法联合mask语义，精确Z值和概率一致性另验。

### 4.3 采集与分割（保留原已选预算作为BASELINE）

第一批A适配器贪心自博弈 **1024局**，按完整局预划分 `896 train / 64 validation / 64 test`。之后固定两轮学生贪心与A适配器贪心的状态补充，每轮**128局**，`96/16/16`，并交替座位与记录冻结A动作/WDL。总基线计划1280局，其中迁移集约1088 train/96 validation/96 test（各轮按局完整划分）。

原采集的所有时间帧必须来自真实合法决策；同局双方与所有帧永远属于一个划分；分别保存执行动作、教师监督动作及来源SHA，不允许混成同一字段。

### 4.4 迁移训练与价值重新校准

先冻结已复制编码器训练新模块2轮，再解冻最多10轮，每次状态补充后最多5轮；采用FP32 AdamW、初始LR `1e-4`、weight decay `1e-4`、有效batch256、梯度裁剪1.0，均为BASELINE。训练目标建议分项监测：`conditional_policy_KL`、`A_greedy_NLL`、`WDL_teacher_KL`，并用验证集确定权重，不能未经实验假设它们天然同尺度。

**迁移保真与search-ready不同**：A的WDL评估的是旧策略/旧时间契约，不应永久作为新规则下的真值。获得初步迁移能力后，用**新规则完整对局的真实终局**校准WDL。先保留策略/共享编码器，优先只优化Value部分；如需解冻共享部分必须增设策略能力回归门槛。目标可写为 `CE(WDLθ,z)+λ_A KL(WDL_A||WDLθ)`，逐步降低`λ_A`，选取策略保持与Value校准同时达标的checkpoint。

### 4.5 迁移验收

原数值门槛保留为初始迁移保真准则：A适配器贪心动作匹配率≥95%；在A选择ACT时完整动作匹配≥90%；WAIT/ACT≥98%；WDL KL≤0.01、标量值MAE≤0.02。**这些统计由专门的诊断贪心解码计算，不等同于随机正式直出。**

实战分两阶段：400局非劣筛选得分≥47%，独立2000局按换边种子对bootstrap评估 `LCB95%≥47%`（原计划表述“达到47%”）；比较新模型**正式随机直出（温度固定）**与A适配器原贪心。差异来自控制器时必须明确报告，而不能把A老师的贪心动作匹配当成随机策略胜率。若认为此跨解码对比不可接受，另加同控制器对照，但不降低已定非劣目标。测试集只在候选定好后使用一次。

迁移通过后导出 `initial.pt`：新优化器、空正式Replay、零正式游标、完整来源清单。失败立即停在迁移门，不自动转入128搜索。

## 5. 搜索：FAIR单世界、结构化候选、128有效模拟

### 5.1 根与候选

在根信息状态 `I`：

1. 从信念 `b(h|I)`抽唯一兼容世界；固定该搜索根使用（真实隐藏世界不得进入这个步骤）。
2. 用自回归策略构造结构化完整ACT：包括当前合法卡牌代表动作、多空间峰值、时间变化、其余随机探索；WAIT单独保留。最多32个唯一候选作为首轮BASELINE，数量和初始开放量可随预算和多样性自适应；旧`min(K,8+floor(N/4))`为对照。
3. 保留模式总质量和各卡牌的原始条件质量。每节点先构建一个**固定的完整候选池** `C_max` 与稳定排序，开放调度只改变可选边，不得在候选逐渐开放时反复重归一化或更改已存边的prior。在有完整候选的card `c`内部，根据原始 `p(l,d|I,c)` 条件归一化：`Ptilde(c,l,d)=p(ACT)p(c|ACT) p(l,d|c)/Σ_{(l',d')∈C_c}p(l',d'|c)`；`Ptilde(WAIT)=p(WAIT)`。如有合法卡牌无法获得候选，需先补入至少一个，不能静默丢弃其质量；若ACT在当前状态无合法完整动作，mode的合法mask应令其概率为0。
4. 候选预算还必须满足 `K_max >= 1 + 当前存在合法ACT的卡牌数量`（右侧1为WAIT）；如果某个8候选实验场景不足覆盖全部合法Card，应自动提升有效K并单独记录，或宣布该实验配置不适用。以上Ptilde是对**候选集的有偏投影**，不是原14万完整动作概率；更不能继续把旧IID有放回抽样的 `p/q` 自动套在“确定性保底+随机补充”的候选集合上。
5. 每根验证 `Σ_{a∈C}Ptilde(a)=1`，WAIT/ACT边际质量在可行条件下守恒，卡牌边际守恒；候选唯一且严格合法。

### 5.2 训练根噪声

采用分层噪声作为研究配置：Mode与ACT内部扰动解耦，第一版优先在ACT候选的**条件分布内**添加Dirichlet噪声，同时不改变Mode的WAIT/ACT总质量；旧扁平全局`ε=0.25,α≈10/K`保留为对照。正式搜索评测**无根噪声**，算法/候选/探索参数全部版本化。

### 5.3 树策略与对手

基线PUCT：`a=argmax(Q+U)`，`U=c_puct Ptilde(a) sqrt(Σ_b N(b))/(1+N(a))`，`c_puct=1.5`为BASELINE。只在己方决策边界执行优化；在对手决策边界按固定版本冻结策略采样，以随机转移处理对手决策及环境事件。模拟必须保持同tick双边观察隔离。对随机树的同一信息节点可多次观察不同结果，Q累计期望；所有Q以根玩家视角记录。

### 5.4 Full、Fast与正式搜索

- **Full**：128次**有效**根模拟；输出 `{(complete_action,N)}`，`sum_root_children N=128`，作为唯一第一版正式策略训练标签 `π=N/128`。
- **Fast**：低预算基线16次；用于推进真实自博弈、生成正常决策时刻的WDL行，**不贡献正式Policy Loss**。
- 初始Full:Fast决策概率 25%:75%，名义平均搜索44次/决策，仅为ABLATION起点；是否启用必须经固定算力与棋力对照。
- **官方搜索评测**：固定128次有效根模拟、冻结同一`search_protocol_id`，无根噪声，按`N/128`采样执行（与原计划一致）。若搜索协议实质改变，不能直接把新128版本与旧协议结果混称同一测量尺度；须将冠军在新协议下重评。

### 5.5 截断：Completed-Decision Boundary Evaluation（LOCKED）

原软预算 `6个事件决策或10秒游戏时间` 保留为对照。若接近边界时某个预约尚未处理完毕，允许在有界额外预算内推进到预约**执行/被覆盖/合法失效**的完整决策边界。若超过额外硬上限，**恢复最近一个完整决策边界**，放弃这段未完成转移与对应边的更新。在该边界计算WDL，回传给所有此前真正完成的搜索边。

不允许：将未完成的(a,delay)当作已完成、将边界前`v(s)`当作该未完成动作的收益、在根动作还没完成时记一次根访问、为了凑够128而填零估值。根未得到有效完成转移时该次尝试不计数，重新采样重试；持续失败隔离根任务并触发错误门槛。**不要由于搜深而无限拖延或无界重试**。

在正常环境推进中，一个ACT预约若被新决策提前覆盖，该预约**生命周期已经结束**；并不要求卡牌真的释放。记录事件原因`executed / preempted / rejected / wait_expired`。具有随机未来事件的部分完成前缀不能跨不同假想环境强行重用快照而改变分布。

### 5.6 搜索输出与限制

`SearchResult`需包含完整合法根候选、`raw_logp`、条件归一化prior、训练扰动后prior、`N`, `Q`,有效模拟数、失败尝试数、搜索世界构造manifest、对手模型ID、实际搜索预算与开销、根FLAG(`deck_revealed`等)。根策略仅来自有效访问次数；第一版不使用Gumbel或先验保持训练目标。

**GATE S**：冻结同一个迁移且价值校准的网络，比较完整对局搜索控制器与直出控制器，预定测试预算和配对种子，完整对局有可证明的正向收益或预先规定的实用优势；卡组揭示前/后只作分层诊断，不强制分别显著为正。未通过不能启动长期128搜索数据生产。参见`ACCEPTANCE_GATES.md`。

## 6. 自我对弈、数据与学习

### 6.1 采集规则

真实对局按双方冻结信息→各自决策→同tick原子提交推进。每局双方Actor/对手版本固定；`actor`和`opponent`可为Champion/历史/Candidate的受控组合。Candidate需合法性、FAIR、基础质量检查后、限额进入Replay；Candidate绝不因进入Replay被自动发布。

每个合法真实**决策时刻**产生 `(I_t, action_executed_or_scheduled, role, search_meta, eventual_z)`；Full行含稀疏`(a,N)`及`π=N/128`；Fast行`policy_target=null`，保留Value监督。任何中间pending状态均不作为额外训练行。中断、协议拒收或证据不完整的整盘隔离；正常预约被新决策抢占的事件保留，不当成异常取消。

### 6.2 联合Loss

`L_policy = -(1/|B_full|) Σ_{i∈B_full}Σ_{a∈C_i} (N_i(a)/128) log pθ(a|I_i)`；用各层conditional `log_prob` 精确计算，不创建14万维密集target。`L_WDL = (1/|B_all|)Σ_{i∈B_all} CE(WDLθ(I_i), z_i)`。两类分母分别按**有效样本数**归一化，不能因为Full只占25%使Policy梯度意外缩成1/4。

`L = λ_p L_policy + λ_v L_WDL + λ_aux L_aux + λ_reg ||θ||²`。BASELINE `λ_p=λ_v=1`，辅助头默认为关；允许只在真实决策帧使用终局塔血/皇冠数做独立辅助消融，不能改变主奖励或采集中间状态。只在真实完成终局后产生`z`，正常win/draw/loss标签从该训练行**行动者视角**计算。

只监测但不默认增加：共享主干Policy与Value梯度范数、夹角、任务损失、分层熵、Mode的WAIT质量、实际出牌率、无效ACT等。不能只因数值loss大就自动平衡梯度。

### 6.3 Replay 与在线训练

- 分层维度：Champion/历史/Candidate来源、近期/较旧、Full/Fast、对局阶段、卡组是否揭示；先按整局均衡配额，再从该局的合法决策帧轮转采样，避免超长局支配。
- 初始容量采用“最近10代/窗口、最多100万行”作为BASELINE。流式架构中`generation`只是可审计报告窗口，不是训练同步屏障。
- 保存每条训练行的行为策略版本、搜索协议、标签协议、对手版本、回放划分、终局证据和必要的随机状态。不同来源绝不能抹除标识后合并。
- 采用一个主训练seed；学习分支保留累计权重/优化器与游标，candidate被否决不能自动丢弃已获得的梯度进度。冻结发布champion另存原子不可变快照。
- 初始优化器/精度：FP32、AdamW、LR`1e-4`、weight decay`1e-4`、有效batch256、梯度裁剪1.0；按独立验证表现调整。梯度累积允许但有效batch语义固定。
- 连续5个可比报告窗口内价值指标未超过训练标签类别频率基线且实战没有改善，暂停并给出诊断；不能因为val的随机波动直接覆盖Champion。

## 7. 评测、统计与发布（LOCKED）

### 7.1 三套身份

`network_checkpoint_id`：参数及特征身份。`search_controller_id`：参数 + 128搜索协议版本。`direct_controller_id`：相同参数 + 温度校准版本 + 采样随机协议。**搜索和直出晋级共享同一个网络checkpoint SHA**，但各自有固定的、预先冻结的控制器配置。

### 7.2 分段评测

1. **离线预筛**：验证WDL与策略、合法性/FAIR、吞吐与禁用项；对明显退化candidate直接淘汰。预筛数据不作为正式胜率证据。
2. **实战筛选**：搜索对搜索400局（双方128、无根噪声、按访问分布采样）；直出对直出400局（使用冻结温度的条件联合随机采样）。原方案筛选得分均须>50%才进入独立确认；比较对象为当期Champion相应控制器。
3. **独立确认**：两个指标各2000局固定样本，提前冻结对手、换边配对种子与解码。按换边种子对bootstrap 10,000次计算95%得分区间。两者单独下界都必须**严格>50%**，才能原子发布新Champion。不可根据中途结果追加/提前停止，不可在正式确认集上选温度或搜索参数。
4. **统计多重性说明**：两个单独95%下界同时过门，是项目已锁定的运营判据；它本身**不等于已经控制了跨指标、跨多次候选比较的总体5%家族误报率**。若需严格整体错误率控制，应在正式统计计划中加入预注册同时置信区间/alpha spending；不得把简单的两项95%误称严格全流程5%。
5. **最终里程碑**：相对于历史1755基线（同新100–3000ms限制），搜索与随机直出分别独立2000局，得分≥55%，配对95%下界>50%，并记录搜索预算和控制器身份。其测量口径与冠军晋级是不同的对手体系，不可混报。

得分定义`score=(wins+0.5*draws)/games`；配对时先计算每个“种子×换边”单元的合计得分，再按配对单位重采样，报告样本量、有效对局、置信方法、区间上下限、棋力差与对手分层表现。

### 7.3 搜索优于直出（Q27）

实施前单独冻结网络，用相同FAIR环境、配对种子和相同对手群体评估 `128-search` 对 `direct`。**不能仅比Policy交叉熵、价值MSE或一次搜出来的访问次数**来代替实战。完整对局为主效应；对手8牌揭示前后分别输出根动作/价值差的分层诊断。早期局面的“隐藏信息真实真值”只能用于评估日志，不能在搜索时选择有利世界。

## 8. 五机拓扑、吞吐与容灾（BASELINE + VERIFY）

既有计划以`.35`为协调/学习节点，五机共享GPU推理服务，actors不各自加载完整模型；其真实网络、GPU数量、内存、NVML、磁盘和版本一致性必须实施前只读核验。推理初始`batch=32, wait≤2ms`、有界队列、按checkpoint SHA隔离，叶端仅需要Value时可跳过策略解码。使用快照、增量FAIR信念与紧凑观测；所有cache键包含状态语义与模型版本，不允许混用。

并发压测顺序：每机`1→2→4→8→16→24→32→40→48→64`，每档至少10分钟；最终候选档完成覆盖早中晚阶段的完整局；选有效完成局与高质量Full标签吞吐最大的稳定档，差距≤3%选低并发。动态调节actor并发、Full/Fast生产配比、推理batch、学习任务与评测配额，不追求空转式GPU利用率100%。

安全门槛：actor主机可用RAM≥32GiB、数据盘余量≥20GiB，中心余量≥200GiB且能容纳两代实测数据；无持续换页；真实GPU运算核验。任务相同原种子最多3次尝试，尝试编号独立；重复失败隔离分片并暂停故障源。不允许故障分片无限自动重试。

正式调度指标：`valid_games/hour`, `valid_full_labels/hour`, 推理请求与GPU时、CPU事件推进数、p50/p95/p99推理队列等待、引擎快照恢复失败、根完成率、FAIR审计拒绝率、数据字节/GPU小时、固定总资源后的棋力增益。

## 9. 生命周期、原子性与不可执行事项

- `capture → validate → commit_episode → replay_admit → train → freeze_candidate → prescreen → confirm → publish` 的每一步使用不可变输入身份、原子完成标志与幂等任务ID。重启后不重复消费整局、不复用错误种子命名空间。
- 保留旧实验数据并保存旧现场；停止/调整旧任务之前先只读检查所属PID、调度租约和持久化游标。本包**没有执行任何这类操作**。
- 迁移数据与正式自博弈Replay隔离；不同验证/test/确认种子空间永久独立。
- 所有失败候选都可追溯；正式Champion只通过原子指针发布或回滚。
- 实施代码必须等所有必要VERIFY/BLOCKER完成并经用户另行授权。本次交付只包含文档与实验计划。

## 10. 阶段放行顺序与依赖

`P0 环境和A只读核验` → `P1 引擎/FAIR/概率不变量` → `P2 A迁移保真与新规则WDL校准` → `P3 单机冻结模型搜索质量` → `P4 小规模搜索蒸馏闭环` → `P5 Full/Fast与Replay消融` → `P6 五机容量与动态调度` → `P7 连续Learner+双冠军指标发布` → `P8 1755最终里程碑`。

具体判据、实验清单、接口和风险依次见：[`ACCEPTANCE_GATES.md`](ACCEPTANCE_GATES.md)、[`EXPERIMENT_MATRIX.md`](EXPERIMENT_MATRIX.md)、[`DATA_CONTRACTS.md`](DATA_CONTRACTS.md)、[`RISK_REGISTER.md`](RISK_REGISTER.md)。

## 11. 明确保留为实验，而非偷换成既定事实

- `25/75`与`128/16`不是已证明最优的预算；正式监督在Full仍是`N/128`。
- `K=32`、`c_puct=1.5`、`6事件/10秒`和额外事件延长硬上限需要控制消融。
- KataGo式辅助终局头、Gumbel完整搜索、经验卡组先验温度、各条件头直出温度、Replay来源配额均不能由直觉声称有效。
- 隐藏状态抽一个世界是接受的预算约束，不保证搜索策略在不完全信息对抗中最优。
- 当前没有源码/真实模型/服务器证据，规格里的VERIFY必须在正式实施前完成。


---

# B. Q1–Q43 决策登记

# Q1–Q43 设计决策登记与冲突消解

> 本表优先采纳用户的最新明确裁决；同编号多轮问法按**最后已确认的真实语义**解释。未被明确投票的Q5–Q9仅在后来已明确锁定的选择可推断时继续采用；对原始引擎的事实不作虚构。

| ID | 已采用的决策 | 级别 / 补充 |
|---|---|---|
| Q1 | **同一checkpoint的搜索和无搜索直出必须双指标晋级** | LOCKED；不拆两个正式冠军 |
| Q2 | 可以废弃扁平联合全局argmax | LOCKED；重定义正式直出为条件联合采样与温度校准 |
| Q3 | 原单世界、128次搜索保留为基线，可优化搜索器 | LOCKED；此后明确**不采用多世界** |
| Q4 | 离线暂停式模拟；AI思考不推进游戏时间 | LOCKED；实际计算仍计成本 |
| Q5 | 曾讨论WAIT是否保留旧计划 | **已由Q12后续明确裁决覆盖** |
| Q6 | 条件联合随机直出；对照T=1，允许逐头校准 | 沿用后续已经采纳的正式直出路径；不再全局argmax |
| Q7 | 主目标为信念下预期胜负，不强制CVaR | 设计目标；单世界是近似实现 |
| Q8 | Candidate可有限额参加受控数据采集，但不能绕过冠军门槛 | 被Q42进一步明确 |
| Q9 | 研究算法按固定总计算预算下的最终训练增益比较 | 冻结模型对局强度单独报告 |
| Q10 | 废弃142,849维扁平Policy Head，采用精确条件自回归概率 | LOCKED |
| Q11 | A的自回归结构保留，符合参数/语义者拷贝，其余蒸馏 | LOCKED；Q13细化 |
| Q12 | 仅WAIT/ACT；没有CANCEL。新决策触发则旧未执行预约作废 | **LOCKED；覆盖此前WAIT保留计划说法** |
| Q13 | 复制语义一致的A解码器参数，其他部分通过条件分布蒸馏 | LOCKED |
| Q14 | 结构化候选B，确保卡牌、空间与时间多样性 | LOCKED；纯抽样为对照 |
| Q15 | **第一版Policy监督使用原始N/128** | LOCKED；不默默换Gumbel目标或先验KL |
| Q16 | 同tick到期执行优先于该tick新决策触发 | LOCKED；需引擎只读核验并建立一致的事件阶段顺序 |
| Q17 | ACT实际执行时重新校验合法性；失败依引擎语义空操作/后续唤醒 | LOCKED；具体事件表VERIFY |
| Q18 | **仅一个信念世界**；不使用4/8粒子正式搜索 | LOCKED；每根独立抽一个兼容世界，128次共享这个世界 |
| Q19 | 结构化候选自适应配额 | LOCKED；固定配额为消融 |
| Q20 | 未揭示卡组时抽取一个FAIR兼容隐藏补全状态 | LOCKED |
| Q21 | 搜索中的对手按冻结策略采样、不做对手侧搜索 | LOCKED |
| Q22 | 曾要求Value评价pending中间状态 | **被用户之后明确否决，改为决策边界估值** |
| Q23 | 合法历史约束 + 经验卡组先验，产生唯一补全世界 | LOCKED；先验需校准并冻结版本 |
| Q24 | 冻结Champion与代表性历史对手池 | LOCKED |
| Q25 | 曾提出采集中间pending状态给Value监督 | **被用户之后明确否决，不得采集中间Value样本** |
| Q26 | 搜索候选先验保留WAIT/ACT与Card质量，再在每张牌入选候选内归一化 | LOCKED；已知有偏近似 |
| Q27 | 先完成冻结网络搜索增益实战验收，再大规模搜集Policy标签 | LOCKED BLOCKER |
| Q28 | 各条件Head可独立进行直出温度校准；T=1对照 | LOCKED；温度配置须随checkpoint冻结发布 |
| Q29 | 搜索只在己方决策节点优化；对手冻结策略是随机转移 | LOCKED |
| Q30 | 自适应候选开放，原`8+floor(N/4)`为对照 | LOCKED |
| Q31 | 有限事件感知延长；仍未完成时回到最近完整决策边界 | LOCKED；不更新未完成边 |
| Q32 | **取消中间状态采集**，只保存正常决策时刻的Value训练行 | LOCKED，覆盖旧Q25 |
| Q33 | 混合Full 128与Fast低预算；仅Full提供Policy监督 | LOCKED；25%/75%，128/16仅实验起点 |
| Q34 | 连续采集+连续Learner+独立冠军发布；512局仅为报告窗口 | LOCKED |
| Q35 | Replay按新旧、来源、对局分层 | LOCKED；权重由实验决定 |
| Q36 | Policy/Value loss默认1:1；允许调整，并测梯度冲突 | LOCKED；不得仅按loss数值自动平衡 |
| Q37 | 轻量预筛；正式双项2000局独立确认要求不变 | LOCKED |
| Q38 | 五机资源感知动态调度，以有效标签、完成局与最终学习增益衡量 | LOCKED；只保留一个训练seed |
| Q39 | 分层根噪声（Mode与ACT内部可分开）；原全局Dirichlet为对照 | LOCKED；初版先只扰动ACT内部 |
| Q40 | 搜索增益门槛看完整对局，未揭示/已揭示分层诊断 | LOCKED |
| Q41 | WDL基线；允许正常决策帧上的终局塔血/皇冠辅助Head消融 | LOCKED；不采集中间状态 |
| Q42 | Candidate数据在FAIR/质量通过后限额进入Replay，并追踪隔离与撤回 | LOCKED；不自动晋级 |
| Q43 | 正式搜索评测使用128次**有效**模拟，同版本搜索协议冻结 | LOCKED；变更协议则重建可比基线 |

## 被后续决策覆盖的旧条款（必须从实施入口排除）

1. **扁平联合Head**：废弃；精确条件自回归Mode→Card→Position→Delay。
2. **联合argmax为唯一直出**：废弃；采用从联合条件分布采样，温度可事先校准。
3. **WAIT保留先前ACT计划**：废弃；新决策触发时旧未执行计划立即作废，无显式CANCEL。
4. **多信念世界是必要配置**：废弃；仅保留一个FAIR兼容补全世界。不要因“牌揭示后确定”忽略早期信息不确定性。
5. **中间状态WDL训练/叶端估值**：废弃；只能在完整决策边界估值。允许在现有决策帧上预测终局辅助信息。
6. **所有决策固定128次**：训练阶段废弃；Full/Fast混合，只有正式搜索评测固定128次。
7. **每512局全系统停止并同步晋级**：废弃；512作为统计窗口；Learner与Champion解耦，冠军仍双指标。
8. **训练时将候选先验重新扁平归一化**：废弃；保持Mode/Card总质量的层级条件重分配。
9. **把64、128等旧搜索参数一概视为完成验证**：禁止；须只读检查、复现实验。

## 已锁定而且不可误用的“环境事实假设”（VERIFY）

- 实际时序引擎确实满足“同tick到期执行先于新决策”，以及新决策触发之前未执行计划的失效边界；如果源码事实矛盾，应停止修订规格、请求用户澄清，不能偷偷改规则。
- 当前手牌数量、未来100–3000ms可支付、18×32的不同卡牌放置限制、重试/空操作的具体引擎回调需要只读确认。
- “对手8张牌都公开后隐藏状态完全确定”仅在标准卡组轮转且没有额外未公开随机性/预约信息时对**牌组与队列**成立，并不自动意味着未来对手行为确定。
- A旧61档delay如何编码、与新30档的真实对齐、教师A特征/网络层语义和原checkpoint实际SHA需要只读核验。
- 当前五机资源、.35协调端、CUDA/NVML与数据盘现状需要实施前测量，本文不声称已经检查。


---

# C. 数据契约

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


---

# D. 实验矩阵

# 实验矩阵与因果对照 v1.0

**状态**：预注册的实验设计与否决逻辑；所有数值结论尚未实测。CSV机器可读版见 [`EXPERIMENT_MATRIX.csv`](EXPERIMENT_MATRIX.csv)。

## 1. 实验纪律

1. **一次改变一个可归因变量**：除非某方案必须成套构成完整算法（如Gumbel搜索），否则不同时替换Policy头、候选生成、MCTS和回放。
2. **正确性Gate优先**：FAIR、概率守恒、终局标签、未完成边回传等错误发生时，不得用胜率侥幸通过替代修正。
3. **预算匹配**：冻结搜索比较统一128有效根模拟；训练效率比较统一GPU小时/CPU事件推进/网络查询/实际墙钟与固定总设备。名义模拟次数不能冒充等算力。
4. **独立数据**：checkpoint选择只用训练与开发验证；正式确认及1755里程碑的种子隔离。
5. **每个实验报告两个实战指标**：搜索与直出；同时记录WDL标定、Full策略CE、WAIT/ACT比例和不同对手/隐藏信息揭示阶段。
6. **只一个训练seed**：实验可多种对局随机seed，不能默认新增多训练seed来榨资源；如要统计重复训练实验需另行批准。
7. **结果状态必须四分**：PLANNED/RUNNING/INVALID/COMPLETE，不运行就不得写成已验证收益。

## 2. 总矩阵

| ID | 阶段 | 核心问题 | 基线 | 新方案 | 放行/否决 | 优先级 |
|---|---|---|---|---|---|---|
| E00 | P0 | 原A导出来源是否真实一致 | 待核验A | 核验SHA、student第128步逐参数、输入语义 | 不一致立即停迁移 | P0-阻断 |
| E01 | P0 | WAIT/ACT计划抢占及同tick顺序 | 现有引擎行为 | 新规范事件契约逐案例重放 | 任一不一致停搜索 | P0-阻断 |
| E02 | P0 | FAIR信息能否间接泄漏 | 相同FAIR信息不同hidden | 固定抽样seed运行根/对手/唤醒/缓存 | 任何泄漏停实施 | P0-阻断 |
| E03 | P0 | 自回归联合概率是否正确 | 小空间穷举oracle | prefix合法mask及条件logprob | 概率守恒、无非法动作 | P0-阻断 |
| E04 | P1 | A61档迁移如何限制到30档 | 逐级mask | 全局合法重归一化对照 | 适配器语义冻结且无漏项 | P0-阻断 |
| E05 | P2 | A策略参数复制还是分布蒸馏 | 新头随机初始化 | 兼容权重复制 + 软条件蒸馏 | 选满足保真门槛方案 | P1 |
| E06 | P2 | 迁移后WDL是否校准新规则 | 仅蒸馏WDL_A | 真实新规则终局再校准WDL | 迁移与WDL两门均达标 | P0-阻断 |
| E07 | P3 | 搜索是否优于无搜索策略 | 自回归随机直出 | 单世界128次结构化PUCT | 正向搜索增益门，否则停正式MCTS | P0-阻断 |
| E08 | P3 | 结构候选是否改善召回 | 旧有放回31候选 | 结构化自适应 K≤32 | 有益且不破概率/FAIR | P0 |
| E09 | P3 | 候选优先质量投影是否偏置 | 直接选中集合归一化 | Mode/Card质量保持条件重分配 | 守恒必过；实战优劣记录 | P0-阻断 |
| E10 | P3 | 32候选数量是否最优 | K=32 | K=8/16/32自适应开放 | 冻结主版本不擅改 | P1 |
| E11 | P3 | 搜索固定展开为何不足 | 原8+floor(N/4) | 自适应progressive widening | 固定总算力下选择 | P1 |
| E12 | P3 | 分层噪声是否优于全局 | 旧全局Dirichlet25% | ACT内部扰动、Mode独立不扰动 | 无公平性和策略漂移异常 | P1 |
| E13 | P3 | 截断是否导致收益错配 | 固定6事件或10s | 有限事件延长+边界回退 | 先过严格正确性，再比性能 | P0-阻断 |
| E14 | P3 | 模拟对手差异会否误导搜索 | 仅当前Champion直出 | Champion+历史冻结对手池 | 选择跨群体更稳版本 | P1 |
| E15 | P3 | 经验卡组先验是否比均匀补全更准 | 合法均匀单世界 | 基于已知历史的经验先验 | 真实hidden仅离线审计 | P1 |
| E16 | P4 | MCTS标签是否学得动 | 仅迁移继续蒸馏 | Full N/128稀疏监督 | 不能只看CE下降 | P0-阻断 |
| E17 | P4 | Full/Fast组合是否省资源 | 全部决策128 | 25% Full128 +75% Fast16 | 若学习效率退化则回退固定128 | P0 |
| E18 | P4 | 分层Replay比均匀更有效 | 按整局均匀 | 按新旧/来源/FULL比例分层 | 无来源污染与测试泄漏 | P1 |
| E19 | P4 | WDL/Policy梯度是否冲突 | λp=λv=1 | 小网格λv/λp、统计夹角 | 不得仅按loss值自动平衡 | P2 |
| E20 | P4 | 终局塔血皇冠辅助头是否有用 | 纯Policy+WDL | 增加终局辅助任务，禁中间帧 | 消融成立才启用 | P2 |
| E21 | P5 | 直出温度需要分别校准吗 | 全部T=1 | Mode/Card/Position/Delay独立验证温度 | 配置随checkpoint冻结 | P0 |
| E22 | P5 | Candidate探索是否带来退化 | 纯Champion采集 | 限额Candidate+历史来源 | 异常隔离可回滚 | P1 |
| E23 | P6 | 连续流式运行是否有实际收益 | 512局严格同步 | 流式Actor/Learner/Champion gate | 满足恢复和双门发布 | P1 |
| E24 | P6 | actor并发和推理batch效率 | 并发1、batch32/2ms | 1→2→4→8→16→24→32→40→48→64 | 选稳定吞吐最高档 | P0-阻断 |
| E25 | P7 | Champion双指标晋级可靠性 | 现任Champion | Candidate冻结，预筛+400+2000各两项 | 同checkpoint两个LCB均严格>0.5 | P0-阻断 |
| E26 | P8 | 最终1755里程碑 | 1755限制100–3000ms | 新Champion搜索及直出独立对1755 | 两项score≥55%且LCB>0.5 | P0-终验 |
| E27 | P3 | 固定预算下根策略替代值不值得 | 结构化PUCT128 | 规范Gumbel或根层配对评估 | 只作为独立研究候选，不替换N/128 | P2 |

## 3. 必须一起提交的成本与质量指标

每项冻结网络搜索对照最少交付以下记录：

- 决策/对局：有效局数、配对胜/平/负、95%区间、双方座位、对手ID、卡组揭示阶段。
- 搜索：有效128根访问计数、根失败尝试、未完成边backup数、候选质量投影校验、不同来源候选召回、`K`、PUCT版本。
- 推理：GPU网络调用数、batch分布、请求排队p50/p95/p99、网络耗时、根决策墙钟。
- 模拟：引擎事件推进总数、快照创建/恢复次数、独立完整局数、硬盘及内存峰值。
- 训练：Full有效标签数、Fast Value行数、按整局样本覆盖、梯度夹角、Value CE、Mode/Card/Position/Delay条件熵。
- 学习效果：**同总算力**下固定对手群体的搜索得分和直出得分，不能仅比较样本生成速度。

## 4. 实验次序与停止规则

- **E00–E04**：纯契约与来源只读+单元断言，任何阻断失败即停。
- **E05–E06**：先迁移与价值校准，不能在蒸馏未过门时评估搜索收益。
- **E07、E09、E13**：首次冻结网络搜索放行；这三个是启动大规模搜索自博弈的关键阻断。E08/E10–E15给出算法改善的选择证据。
- **E16**：验证搜索数据让学生学会并产生实战增益；失败先看标签和解码而不是堆算力。
- **E17–E22**：在小规模闭环已成立后优化预算、回放、损失、温度。
- **E23–E24**：只有前面质量门过后才五机放量。
- **E25、E26**：Champion正式两项晋级及1755终验，数据严格独立。

## 5. 单世界专门报告

坚持Q18：正式搜索只抽一个世界，**不设多世界正式对照作为阻断**。这不会令单世界结果无偏；应报告经验先验校准、揭示前后的搜索增益、真实对手群体失配、根动作随抽样随机数的敏感性（这是离线诊断，**不是在一次根搜索中多粒子投票**）。当诊断显示强偏差时，先改先验与搜索标注/使用规则，不私自修改已锁定的世界数量。

## 6. 结论如何填写

建议每个实验报告：`hypothesis`、`network_sha`、`fair_sha`、`engine_sha`、`search_protocol_id`、`train_seed`、`paired_seed_namespace`、`baseline_result`、`variant_result`、`compute_budget`、`paired_CI`、`invalid_cases`、`decision`、`reviewer`。没有数据时明确写`NOT RUN`，不得以历史项目的58.45%口径冒充现协议结果。


---

# E. 分阶段验收门槛

# 分阶段验收、阻断条件与统计门槛 v1.0

**声明**：以下为应执行的门槛而非已执行的测试。数值来自用户原计划与Q1–Q43裁决；尚未核验引擎和五机。未通过Blocker不得由调度器自动跳过。

## G0 — 来源、规则与运行现场的只读核验（BLOCKER）

| 检查 | 通过条件 | 失败动作 |
|---|---|---|
| A来源 | 导出文件SHA为`e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015`，旧学生第128步身份逐参数/缓冲区对应，特征与catalog同一语义 | 禁止迁移，保存差异 |
| FAIR | 允许公开历史、己方私有控制状态；不允许真对手手牌、牌序、私有seed、尚未公开计划进入策略链 | 禁止采集 |
| 动作时序 | 旧指令在更早的新决策触发时作废；同tick到期先结算；执行时复核合法；双边观察原子冻结 | 停机验规则；不要修饰测试结果 |
| 时间语义 | WAIT最大3秒；ACT延迟100–3000ms；0ms与>3000ms被屏蔽 | 禁止生成正式数据 |
| 环境资源 | 五机挂载/可用盘/CUDA实际运算/进程归属/调度租约核验 | 只诊断，不改动旧进程 |

> 这里不认为“用户给出的引擎语义”已经被源码验证。若实际引擎规则与规格冲突，暂停修改并提交差异给用户裁决。

## G1 — 数据与数值正确性（BLOCKER）

- **完整动作**：所有合法action ID都可以可逆编码/解码；当前序列化catalog与坐标变换一致；非法动作概率质量与实际执行数均为0。
- **条件联合**：在可穷举的小空间 `|Σ_legal p(a)|−1|≤1e-5` (FP32基线)、各前缀无空合法后缀，`log_prob`与采样分布在统计容差内吻合。FP数值阈值是初始工程门槛，可根据实际运算形式校核，**不得因失败而放宽FAIR或合法性**。
- **旧61→新30**：每个合法延迟映射无偏移；逐级mask与全局联合截断的差异被显式量化；适配器选择的语义冻结并可重演。
- **候选投影**：小空间穷举对照，`sum Ptilde=1`、`Ptilde(WAIT)=p(WAIT)`，每张有合法ACT的卡被覆盖，原每卡概率质量守恒；`K`内候选去重，旧`p/q`不能被错误复用。 候选池`C_max`一旦确定，渐进开放时不得重新归一化已存边先验；`K_max`不能小于`WAIT + 合法卡牌数`。
- **价值视角**：胜/平/负和WDL根玩家视角绝对一致；即使双方同tick行动、或一方多次唤醒也不机械翻转。
- **完成决策边界**：以每个事件类型做严格回归；任何未完成根边不得计入模拟预算；`unfinished_edge_backup_count=0`。
- **搜索计数**：每个合法Full根`sum root N==128`，Fast根从不填充伪 `N/128`；重复恢复不重复计访问。
- **FAIR**：相同FAIR输入/相同belief seed但不同真实隐藏，对根候选、动作、唤醒时序和模拟可见字段非干扰；真隐藏值仅审计区可读。

## G2 — A迁移保真与非劣（BLOCKER）

### G2A：完整局分割与teacher fidelity

基线数据：A直出1024局=896训练/64验证/64测试，学生对A状态补充两轮×128局，各96/16/16；已完成局、双方记录、状态与源SHA对应，测试只在最后使用。

原定迁移指标：

| 指标 | 必须达到 | 解释 |
|---|---:|---|
| 完整贪心动作与A适配器一致 | ≥95% | teacher fidelity诊断，非正式随机直出 |
| A选择ACT帧的完整动作一致 | ≥90% | 检查模式以外参数 |
| WAIT/ACT一致 | ≥98% | 模式精度 |
| WDL teacher KL | ≤0.01 | A→学生蒸馏误差 |
| WDL标量值MAE | ≤0.02 | 按同一win-loss方向计算 |

必须按阶段、双方座位、卡牌、位置与延迟分组，不能总平均掩盖某类卡牌严重偏差。教师的贪心硬标签与soft条件分布都要测，且不因正式随机执行而取消teacher fidelity测试。

### G2B：正式随机直出实战非劣

- 用冻结温度的学生随机直出对冻结A适配器贪心：400局筛选，得分≥47%。
- 独立2000局换边配对确认，10,000次配对bootstrap下界≥47%。
- 提供`score`、LCB/UCB、W/D/L、按座位/种子/公开卡组揭示状态诊断；如模型无法在不同执行规则下满足非劣，就报告并停在迁移阶段，不将teacher accuracy替代实战。
- 新规则真实终局WDL校准必须有独立验证改善或不显著退化证据，才允许搜索输入；不能只复制A旧WDL就当作新规则下的准确排序器。

## G3 — 冻结网络搜索增益（BLOCKER；Q27）

**先有证明，再开始大量128次自博弈。** 固定迁移且校准后的同一checkpoint，对比：直出(T冻结) vs FAIR单世界128有效模拟结构化搜索，在同样的预注册对手群体和换边场景下的完整对局表现。建议400局探索评测+独立2000局确认；以**配对得分差的95%置信区间下界大于0**作为首版显著改善标准（方案性统计门槛，如测试效力不足不得自行宣称通过）。

必须同时附：冻结网络、对手模型ID、数据种子、无正式根噪配置、128有效访问审计、引擎推进/推理墙钟成本、卡组未揭示/揭示后的分层诊断。揭示两个分层不要求分别显著正收益，整局主效应为准。

若失败：依序调查真实概率/mask→候选覆盖→截断回退→对手模型失配→Value校准→单世界误差；**不得通过增加GPU或无证据延长训练跳过此门**。

## G4 — 单机完整闭环（BLOCKER）

- 使用迁移模型的**测试副本**完成至少32局真实完整单机闭环。
- 从冻结网络→Full/Fast搜索→原子记录完整局→终局填标签→分层Replay→训练SGD→中断恢复→候选评测，全路径可走通。
- 测试得到的网络与数据留在验收目录；正式第0轮必须从迁移验收`initial.pt`重新启动，不能把测试训练权重混进去。
- 梯度在Policy/WDL两个头及共享主干确有更新；仅Full有Policy监督；不采集中间pending Value；resume后优化器、RNG、游标及标签预算对应完全一致。
- 至少完成一次“训练Policy CE改善≠自动声称实战改善”的独立冻结对局验证。搜索改进能否通过蒸馏传给直出，需要实测。

## G5 — Full/Fast与在线Replay效率（质量门）

- 基线固定128次每个决策，与候选`25% Full128 / 75% Fast16`比较。
- Full稀疏标签`ΣN=128`；Fast只有真实决策时刻WDL；Loss分别按有效Policy/Value样本数归一化。
- 对**相同总CPU/GPU计算预算**后的搜索+直出实战分别报告；不同配置没有稳定优势时保持固定128基线，不以名义模拟次数少65.625%推断真实训练加速。
- Replay按来源、近期、Full/Fast、整局均衡采样；确保Candidate错误可隔离/回滚；无任何完整局跨train/validation/test泄漏。

## G6 — 五机容量与恢复（BLOCKER）

| 资源/任务 | 初始门槛 |
|---|---|
| 每actor主机内存 | 至少32GiB可用；无持续换页 |
| 每actor主机数据盘 | 至少20GiB剩余 |
| 中心汇总盘 | 至少200GiB剩余，并能实际容纳两代数据 |
| GPU/推理 | 必须真实CUDA推理成功；共享服务按SHA隔离；batch初始32、等待2ms仅起点 |
| 并发实验 | 每机1→2→4→8→16→24→32→40→48→64，每档≥10min，最高稳定档跑完整局 |
| 自动重试 | 同比赛种子≤3次尝试，每次attempt独立身份，失败暂停分片 |
| 幂等 | Replay提交/训练消费/评测去重，断点恢复不制造额外有效对局 |

选择稳定、有效完成局与Full有效标签产能最高档，若差异≤3%选较低档，不能只最大化GPU利用率。容量失败不得自动部署其他节点假定安全。

## G7 — 同一checkpoint双指标正式Champion晋级（BLOCKER）

1. Learner持续训练，定期冻结**一个**可重演Candidate checkpoint；预筛离线正确性与验证指标。
2. Search-vs-search 400局（双方相同冻结128次无噪协议、都从`N/128`采样）。Direct-vs-direct 400局（双方冻结校准温度条件采样）。只有两组筛选得分都>50%才进入独立确认。
3. 每组各**独立2000局**确认，预定换边配对seed。配对种子对是10,000次bootstrap重采样单位；每组 `LCB95%(score)>0.50`。
4. 两组必须同时满足，且search/direct引用的**network checkpoint SHA完全相同**；否则不给Champion身份。失败不能删除Learner累计进度，但也不能让其悄悄发布。
5. 不能在确认过程中反复调温、调搜索参数、提前停局、重用确认seed挑模型。若正式搜索协议改变，现任Champion同协议下重评或新建评测尺度。
6. **统计声明**：两个独立95%下界同时通过是本项目操作性门槛，不代表跨两个指标和无限多Candidate的总体5%误报概率；如果需要严格控制家族错误，应另外注册联合区间/错误率预算。不得将现门槛过度宣传。

## G8 — 对历史1755基线的最终验收

场景：双方动作允许100–3000ms。**同一Champion network_sha**：

- 官方128搜索 vs 历史1755：独立2000局，`score≥0.55` 且 `LCB95%(score)>0.50`。
- 校准温度条件随机直出 vs 历史1755：另独立2000局，`score≥0.55` 且 `LCB95%(score)>0.50`。
- 输出真实对手/双边座位/卡组阶段/按卡牌局面分组统计，并附实现、FAIR、引擎、源A、搜素协议、checkpoint和controller身份。
- 历史58.45%仅是旧评测口径，不能直接作为新协议已经通过的证据。

## G9 — 必须交付的验收材料索引

- **Source**：SHA/feature/catalog对账、旧现场只读清单。
- **Engine**：动作时序测试、same-tick并发测试、FAIR非干扰、合法延迟与拒绝条件。
- **Neural**：自回归概率穷举测试、A迁移分层保真、WDL校准与训练恢复。
- **Search**：128有效访问与截断边界audit、结构候选质量守恒、冻结网络搜索增益报告。
- **Training**：Full/Fast数据及预算对照、Replay来源采样、学习曲线与梯度诊断。
- **Systems**：五机容量/共享推理/失败恢复/原子发布报告。
- **Evaluation**：迁移400/2000、Champion400/2000双项、1755双项2000、所有配对seed及置信区间。

> 所有Gate状态默认`NOT RUN`。执行日志必须带`GATE_ID`, `test_manifest_sha`, `time`, `pass/fail`, `evidence_artifact`, `reviewer`。不允许仅将本规范文件存在仓库里就标记为通过。


---

# F. 实施顺序与交付计划

# 实施顺序、系统调度与交付工作分解 v1.0

**本文件只描述未来实施安排；没有对服务器/代码/实验任务采取任何操作。** 施工前必须先获得独立实施授权。依赖与验收Gate见 [`ACCEPTANCE_GATES.md`](ACCEPTANCE_GATES.md)，详细协议见 [`TRAINING_SPEC.md`](TRAINING_SPEC.md)。

## 总体路径和并行边界

```text
P0 现场只读审计 -----------┐
                          ├─> P1 FAIR/时序/联合概率契约
P0 A身份及特征核验 --------┘          │
                                     v
                              P2 A迁移+WDL校准
                                     │ G2
                                     v
                             P3 冻结网络搜索实验
                                     │ G3
                                     v
                         P4 单机32局自博弈/训练闭环
                                     │ G4
                                     v
                    P5 Full/Fast、回放、梯度与温度消融
                                     │ G5
                                     v
                           P6 五机并发/容灾/调度
                                     │ G6
                                     v
                         P7 连续Learner与正式Champion
                                     │ G7
                                     v
                             P8 1755最终里程碑 G8
```

并行允许：P0的GPU诊断与A源码只读核验、P1的FAIR规则测试与概率单元测试、P3冻结网络的不同search runner相互独立测试。禁止：在G2之前大规模搜索数据采集；G3之前全力五机放量；G7之前发布新Champion。

## P0 — 保存旧现场、获取只读证据

### 输入与操作边界

- 列出旧调度器、正在运行的任务及其归属，不猜测PID，也不停止不属于本项目的进程。
- 读取旧source checkpoint、旧学生第128步参数、代码commit、catalog与feature schema；计算实际SHA256并对账。本轮已有SHA只作待核验输入。
- 只读盘点五机的CPU/GPU/内存/数据盘/挂载/网络与CUDA/NVML实际功能、共享推理服务版本；`.35`中心角色以用户旧方案为基准但必须核验。
- 将旧实验的checkpoint、结果、日志和训练游标位置列出；旧实验运行状态必须按实施当天重新核验。

### 应生成（未来实施）

`source_a_manifest.json`, `runtime_inventory.md`, `old_experiment_inventory.md`, `fair_engine_contract.md`, `source_compare_report.md`。

**不做的事**：不停止进程、不修改任何旧checkpoint、不清空Replay、不用错误驱动库告警推断GPU可用或不可用。

## P1 — 环境与FAIR协议定稿

### 1. 动作与计划时序

先实现可回放事件测试基架而不是优化模型：

- `WAIT/ACT`共用预约生命周期，新决策出现时废弃旧预约。
- 同tick预约到期与新事件，预约先执行；双方冻结同tick合法观察，各自决策后统一提交。
- ACT预约到期重新检查资源、位置与手牌合法性；明确拒绝原因及下次唤醒条件。
- 0ms是内部保留编码但非法；100–3000ms的31旧档/30实际档映射无off-by-one；手牌动态slot与稳定Card ID一一对齐。

### 2. 自回归概率与合法前缀

实现需要的逻辑API（实际命名可不同）：

`JointPolicy.sample`, `.log_prob`, `.conditionals`, `.prefix_mask`, `.topk_conditionals`。对小空间做穷举归一化、路径对比与采样检验，校准教师旧61档的精确定义。

### 3. FAIR负例自动化

基于相同合法信息构造不同真实隐藏状态，检查网络输入、候选、模式概率、唤醒、缓存、模拟对手的合法观察，并验证同tick同时行动不能读取对方尚未公开动作。

### 完成标志

G0/G1全部绿灯，源码差异清单为空或已获明确裁决。不得因训练能跑通而忽略这些测试。

## P2 — A迁移与可搜索价值校准

### 1. 重用旧A

- 对语义一致的Encoder和自回归条件Head逐参数、逐buffer校验后复制。
- 对无法兼容的新主干和Head，通过冻结A的条件分布+WDL软标签蒸馏；旧标量价值头和旧优化器保留为历史，不移入新正式optimizer。
- 教师A分别提供`greedy_complete_action`、`conditional_policy`, `WDL`；真实环境执行动作单独保存。

### 2. 采集基线

`A直出1024局(896/64/64)`；随后两轮状态补充`128局/轮(96/16/16)`，双方位置交换、对局种子与三个用途划分预注册。整盘完成后才能入库，测试集最后才读。

### 3. 蒸馏优化

第一段复制Encoder冻结2轮、新头训练；第二段解冻最多10轮；每次补充最多5轮，验证早停。保留已选的FP32/AdamW/batch256/clip1.0作为可比基线。

### 4. WDL校准

基于新规则真实终局标注，与A旧WDL蒸馏目标加权，先校准价值分支，再在策略保持门槛下允许小步共享主干调优。不同于中间状态采集：**只从完整对局的真实决策边界获得训练行**。

### 5. 迁移验收

运行teacher贪心保真、WDL KL/MAE、400+2000非劣。导出`migration_fidelity.pt`、`search_ready.pt`和最终`initial.pt`，三者身份可不同，正式初始化采用通过所有迁移门的最终权重。训练/验收目录和正式新Replay隔离。

## P3 — 冻结网络的128次搜索验证

### 1. 正确搜索器

实现FAIR单世界采样、结构化完整候选、自适应开放、Mode/Card质量投影、PUCT、仅己方节点优化、冻结对手采样、事件边界截断/回退以及严格128次有效根访问。所有root结果有世界、候选、访问、视角、未完成边审计。

### 2. 必须先跑的测试

- 小动作空间校验卡牌/WAIT概率质量、合法性与输入伪共享。
- 完整动作root的有效完成/抢占/拒绝路径回传。
- 搜索根N严格为128；非法根有限重试后隔离，不可填充。
- 单世界公开阶段的非干扰与对手完整信息权限测试。

### 3. 质量实验

同冻结权重与对手群体，先测自回归直出，再测128结构化搜索。对照旧31个IID有放回候选、固定K开放规则、根噪声分层/全局、硬截断/软延长。扩展方法如Gumbel必须实现为完整独立协议，**不得悄悄替换Q15的N/128监督**。

### 放行

G3通过才能批量生成搜索标签。若搜索不强，必须先查证据链，禁止无目标延长训练。

## P4 — 单机端到端32局演习

- 用正式初始checkpoint的**副本**走32完整局，混合Full/Fast，完整事件/终局/标签可恢复。
- 学习器能在隔离验证数据上降低Policy/WDL loss，且网络共享主干梯度非零；快照恢复后状态/随机流/预算可复现。
- 同局双方的结果、视角、版本一致；政策标签只来自Full，Fast策略标签为空，中间pending状态无训练行。
- 训练演习得到的checkpoint不得污染正式`initial.pt`或正式Replay；记录实验目录后重置到正式初始状态。

## P5 — 训练策略消融和Replay

### 1. 固定算力比较

`全128` 对 `Full128 25% + Fast16 75%`，比较固定总资源后的**搜索和直出**两项棋力、非劣性、有效Full标签吞吐与数据分布覆盖。若混合方案在可比预算下无法改善总体学习，保留全128作基线，不为了简化而强制部署混合。

### 2. Replay分层

落实按整局来源/近期/full-fast分层，记录Candidate来源与隔离策略，不允许旧Champion大量样本淹没近期有效数据；也不能用全部新数据致使历史弱点迅速遗忘。值头训练对所有真实决策时刻有效行计算，Policy只对Full行计算；分别归一化两项loss。

### 3. 头温度与辅助任务

头温度在专用开发验证局拟合，每个候选正式确认之前完全冻结。纯Policy+WDL为基线，终局塔血/皇冠附加头只在实际决策时刻做消融；如辅助头影响WDL/Policy主目标，应直接回退。不得新增中间pending采样。

### 4. Loss调优

以`λp=λv=1`为默认，比较小范围权重比和梯度夹角；最终以固定资源下双方实战强度判定，不能因两loss绝对数值不同而机械调整。

## P6 — 五机容量、推理服务与资源调度

### 1. 初始部署拓扑（需核验实际硬件）

- `.35`拟承担协调、数据汇总和单GPU FP32 learner；其它机器/本机空余资源作为actor与推理worker，具体依实际测得的CPU/GPU/内存/磁盘重排。
- actor不重复加载完整模型，按checkpoint SHA从有界GPU推理服务获取结果；叶端只用WDL时允许跳过Policy条件Head。
- `FullAction`在actor端最终合法复核；推理服务不能决定是否读取未公开真隐藏信息。

### 2. 自动并发Admission

依照原阶梯逐档压测并量化：总引擎事件推进、有效完整对局、有效Full Policy标签、网络推理请求、CPU核心饱和情况、GPU排队、快照恢复、RSS峰值、磁盘写入与失败重试率。

建议调度优先级：

1. 保证**已启动完整局**按预算完成，除非违反资源/正确性门槛。
2. 限制推理服务排队并依据GPU有效吞吐调整batch与actor admission。
3. 为Learner保留稳定梯度计算窗口，但允许依据Replay供应调整训练频率，避免CPU actor被长期GPU等待拖垮。
4. 保留严格正式确认评测的独立资源和seed，避免训练变更污染评测。
5. 内存/磁盘/驱动不健康时停止新增任务，完成可安全收尾工作并保存证据；故障从对应阶段恢复而非重新启动整盘已提交记录。

**调度目标**：最大化固定资源后的可测棋力增益，在运行期以有效局、Full有效监督和健康失败率作代理；不追求每秒GPU占用恒100%。

### 3. 背压与公平

当Replay写入速度>learner消费、GPU推理积压、或搜索Actor抢占评测资源时，应通过减少新actor admission、缩小非必需Full采集或限制推理队列来保护质量与恢复；不可无条件提高并行度。采用租约、回压、任务幂等key、原子commit避免跑满资源产生大量无效局。

## P7 — 连续训练与双指标Champion发布

1. Learner按照单训练seed连续更新，回放按冻结数据窗口与来源分层；定期保存恢复快照。
2. 定期冻结一个Candidate权重及两个控制器manifest，预筛指标异常立即拒绝。
3. Search-vs-Search（固定128无噪、N/128采样）与Direct-vs-Direct（冻结各头温度联合采样）各400局筛选，只有双项均过50%进入确认。
4. 每项各独立2000局，换边种子对配对bootstrap 10,000次，两项score区间下界各>0.5，且两个controller包含**相同network SHA**，原子发布Champion。
5. 未通过的Candidate可持续作为受控探索数据的有限来源，但不可以改变Champion发布指针；训练进度继续保留。源模型有错误时按来源隔离对应数据并记录处理历史。
6. 如果搜索协议更换或评测温度重新校准，视为新的控制器协议版本，先完成同规则旧Champion基准测试再比较。

## P8 — 最终1755基线里程碑

对历史1755在100–3000ms限制下，搜索与直出分别独立2000局，按同一权重checkpoint及固定控制器协议计算两组score；两项都必须≥55%，配对95%下界>50%。报告来源、对手群体、座位交换、FAIR卡组揭示前后、硬件和计算成本。**历史A的58.45%属于旧口径，禁止混入。**

## 可交付成果清单（全部为未来实现产物，当前没有生成）

| 阶段 | 未来实施产物 | 验收证据 |
|---|---|---|
| P0 | 源A证据清单/设备只读盘点 | 来源SHA、旧学生对账、运行环境 |
| P1 | FAIR引擎时序及条件概率测试 | 可重演错误用例、动作ID和mask检查 |
| P2 | `migration_fidelity`, `search_ready`, `initial` checkpoint及迁移报告 | 迁移保真+400/2000非劣 |
| P3 | FAIR128搜索服务、结构化候选、SearchResult协议 | 根128有效、边界回退、配对搜索增益 |
| P4 | 测试32完整局、Trainer/Replay端到端恢复入口 | 不污染正式初始权重的数据隔离 |
| P5 | Full/Fast与Replay训练消融、联合损失实验 | 固定预算最终双项棋力 |
| P6 | 五机动态资源报告与持续调度器 | 容量/健康/背压/故障恢复 |
| P7 | 可发布Champion、双指标晋级报告 | 同SHA两项各2000局独立确认 |
| P8 | 对1755最终双项基线报告 | 每项≥55%与LCB>50% |

本次只创建本设计包中的Markdown/CSV，不触及此表列出的训练产物。


---

# G. 风险登记

# 关键风险登记、监控与回退策略 v1.0

风险等级：**CRITICAL** 正确性错误/泄漏/评测失真；**HIGH** 系统持续学习可能退化；**MEDIUM** 性能或数据效率不达标。均为**实施前潜在风险**，不是已观测事件。

| ID | 等级 | 风险 | 触发/反例 | 必须监控或测试 | 最小修复与回退 |
|---|---|---|---|---|---|
| R01 | CRITICAL | 同tick读到对方未公开动作 | 一方决策在另一方提交后观察更新 | 双边冻结快照、提交时间戳、动作非干扰 | 恢复原子提交，停正式数据 |
| R02 | CRITICAL | 真实隐藏手牌经信念补全泄漏 | 两份相同FAIR不同真值导致根选择不同 | 固定世界采样seed后的反事实非干扰 | 隔离真实seed/private state，停搜索 |
| R03 | CRITICAL | 对手模拟读到己方私有控制计划 | 冻结对手模型输入包含真实未公开计划 | 双边FAIR view字段级白名单 | 改对手view构造并重放审计 |
| R04 | CRITICAL | WAIT/ACT覆盖语义不符合引擎 | 提前唤醒后旧预约仍执行；同tick次序相反 | 同tick/抢占/拒绝/到期事件回放 | 先冻结规则差异，未经裁决不得改源码 |
| R05 | CRITICAL | 未完成延迟动作获得Value | 6事件截断后给a2使用v(I2) | unfinished_edge_backup_count=0、根完成不变量 | 回退最近完成边界；根无效重试/隔离 |
| R06 | CRITICAL | 根128有效模拟被失败访问填充 | timeout、恢复失败仍增长N | 每根ΣchildrenN=128，valid_attempt审计 | 失败尝试单独计数并隔离，无伪标签 |
| R07 | CRITICAL | 自回归条件mask不合法或概率不守恒 | 先选了无法合法完成的Card/Position | 玩具穷举、prefix viability、真实合法率 | 退回已通过单元测试的mask协议 |
| R08 | HIGH | 旧61档截断改变卡牌/WAIT质量 | 逐级renorm和完整联合截断不同 | 教师各级边际与新条件分布对账 | 明确adapter语义、冻结版本，不虚称相同 |
| R09 | HIGH | ACT稀释让贪心过量WAIT | ACT总概率高但单个动作低 | mode mass、ACT率、资源等待、直出胜率 | 改正式为合法条件联合采样+校准；禁MAP再发布 |
| R10 | HIGH | 候选集排除最优动作 | 128模拟都无法选中漏掉的动作 | Recall@K、逐卡/位置/时间覆盖 | 自适应结构化候选与参考搜索复查 |
| R11 | CRITICAL | 候选投影错误放大WAIT | 简单把WAIT .2与ACT选中.02归一 | 质量守恒、小空间枚举 | 保留Mode与Card质量，在每卡候选内投影 |
| R12 | HIGH | 结构化候选混合后误用旧p/q | deterministic elite误当IID有放回 | Proposal日志、采样实验、小空间oracle | 明确候选投影是有偏近似，取消错误校正 |
| R12a | HIGH | 逐渐开放候选时重归一化改变既有边prior | 同一个节点新增动作后已存动作P突变，PUCT统计不可比较 | 固定C_max先验哈希、开放前后P恒等 | 只动态开放固定池，不重写已有prior |
| R13 | HIGH | 一个补全世界带来的信息偏差 | 未揭示牌组阶段搜出只克制抽中卡组的动作 | revealed分层、根采样敏感性/后验校准 | 优先改经验先验与早期采用搜索规则；不擅自多世界 |
| R14 | HIGH | 冻结直出对手与真实搜索对手失配 | Search-vs-search弱于预估 | cross-opponent配对、历史群体得分 | 更新冻结对手模型池与评价群体，不直接重写minimax |
| R15 | HIGH | 迁移成功但WDL不适应新3秒规则 | teacher KL低而新规则输赢误差大 | 真实终局WDL CE/ECE、动作反事实排序 | WDL真实结果校准，策略先冻结 |
| R16 | HIGH | Sparse N/128监督导致策略坍缩 | 未覆盖动作概率持续下降、WAIT过量 | 未访问合法质量、策略熵、实际ACT率、直出棋力 | Q15先维持基线；不足则提交后续修改提案，不私换目标 |
| R17 | HIGH | Fast样本冲淡Policy损失 | Full仅25%，直接整批平均导致Policy梯度缩小 | 每项有效样本数、共享梯度 | 先对Full/All分别归一化 |
| R18 | HIGH | Full/Fast导致Value分布偏斜 | Fast对局过弱，价值样本质量下降 | Value CE、对手分布、同算力最终实战 | 提高Fast预算/Full比例或退回全128 |
| R19 | HIGH | 同checkpoint双晋级僵住 | 新candidate某项进步但另一项未显著 | Candidate通过率、Champion停滞时间 | Learner持续，Candidate限额采集；Champion仍严格双门 |
| R20 | HIGH | 多候选反复确认导致误报 | 对同种子频繁调温或重复检验 | 预注册种子、candidateID、确认次数、alpha记录 | 确认封存、控制器冻结；统计报告注明整体误差限制 |
| R21 | HIGH | Replay数据/评测交叉泄漏 | 同局不同帧或同seed不同attempt分配到两集合 | game_id/seed_namespace去重、split hash | 隔离整局/重复attempt，重评受影响结果 |
| R22 | HIGH | 候选数据难以撤回 | 未晋级candidate有协议错误仍参与更新 | provenance、dataset manifest、admission日志 | 按来源隔离、重新构造Replay并追踪被污染checkpoint |
| R23 | MEDIUM | 温度配置和权重版本不一致 | 相同权重不同温度被当同一控制器 | controller_id/temperature SHA | 固定评测协议，确认不调温 |
| R24 | HIGH | 五机过度订阅引发系统性异常 | 高并发导致CPU换页、GPU等待、磁盘满 | 有效吞吐、queue tail、OOM/swap、快照失败 | 停止admission、降并发、保留完成局、按租约恢复 |
| R25 | MEDIUM | 14万稠密target/日志膨胀 | 每行全FP32 target造成超大存储 | 每行字节、压缩率、I/O峰值 | 改存稀疏(a,N)及重建协议mask |
| R26 | HIGH | 价值视角错误 | 同tick事件或连唤醒时按交替步翻号 | 终局样本角色一致性/根视角审计 | 以玩家ID转换视角，弃错误训练集 |
| R27 | HIGH | 一直增加搜索预算却没有更好监督 | 搜索比直出未显著强 | 独立G3冻结网络配对实战 | 不放五机，先修搜索质量链 |
| R28 | MEDIUM | 辅助终局目标破坏主目标 | 预测塔血好但真实胜率下降 | 双指标实战、WDL校准 | 关闭辅助Head，保持WDL主目标 |

## 必须采用的错误处理优先级

1. **CRITICAL**：即刻停止相关数据进入正式Replay；保存所有证据与输入状态，切断问题checkpoint的推理/数据提交权限；保持现任Champion不动。
2. **HIGH**：暂停受影响算法实验/actor来源，保留可继续的合法数据流；调度器不能自动“为了吞吐”绕过异常。
3. **MEDIUM**：允许以受控消融比较修复并保留基线，强度未证实前不得宣称升级。

## 长期监测仪表盘最小集合

| 分类 | 必备曲线/统计 |
|---|---|
| 搜索质量 | 搜索-直出配对得分差；按完整卡组揭示前/后分层；候选Recall@K；根N严格128比例 |
| 行为 | WAIT/ACT实际占比；卡牌多样性；空间与时间熵；动作非法、抢占与出牌成功率 |
| FAIR | 与真实隐藏值的非干扰抽样测试失败率；不合法唤醒；对手输入字段异常 |
| 价值 | Value CE/ECE；WDL预测vs终局；不同阶段/不同对手的校准；反事实价值排序 |
| 蒸馏 | Full样本策略CE；稀疏目标未访问质量；随机直出胜率是否随着学习同步改善 |
| 统计 | 同SHA双项Champion晋级率；2000局下界分布；确认集访问次数/用途 |
| 资源 | valid games/h、Full policy labels/h、GPU排队p95、CPU引擎推进量、内存/盘峰值、隔离/重试 |

## 三条强制回退原则

- **不能修复最早的正确性问题，就不能把后阶段的训练结果视为有效**。如果FAIR泄漏导致搜索强度虚高，重新训练WDL不会解决问题。
- **不能为了过严格发布门槛而放宽同checkpoint、2000局独立确认或1755指标**。只有用户另行改变验收契约才能改。
- **算法变更必须可逆**：基线网络、候选、搜索、Replay采样、温度、评测版本和数据manifest全部可对照回滚，不通过蒸馏新标签覆写旧源档。


---

# H. 参考资料

# 论文与方法学依据（以及不能从论文直接推出的结论）

**说明**：本项目是皇室战争FAIR隐藏信息、延迟预约、同tick决策、有限搜索预算的自定义环境。以下研究是相关方法的证据来源，**并不意味着这些论文已经在本环境验证了当前配置**。

| ID | 研究 | 可信来源 | 对应内容 | 适用性边界 |
|---|---|---|---|---|
| R1 | Silver et al. (2018), *A general reinforcement learning algorithm that masters chess, shogi, and Go through self-play* | https://doi.org/10.1126/science.aar6404 | 共享Policy-Value、自博弈、MCTS访问策略监督、胜负Value | 原任务为完美信息轮流游戏，本环境为部分可观测、同时事件、带延迟 |
| R2 | Silver et al. (2017), *Mastering the game of Go without human knowledge* | https://www.nature.com/articles/nature24270 | AlphaGo Zero联合训练与搜索目标 | 不包含我们的隐藏状态或超大条件动作细节 |
| R3 | Wu (2019/2020), *Accelerating Self-Play Learning in Go* / KataGo | https://arxiv.org/abs/1902.10565 | Full/Fast混合搜索预算、目标剪枝、额外终局监督、训练效率 | 本方案第一版仍使用原始N/128，不直接照搬目标剪枝；本环境的辅助信号需独立消融 |
| R4 | Hubert et al. (2021), *Learning and Planning in Complex Action Spaces* / Sampled MuZero | https://proceedings.mlr.press/v139/hubert21a.html | 有限候选动作采样与搜索、候选先验修正 | 原先的采样权重推导不自动适用于确定性elite与随机混合的结构化候选 |
| R5 | Danihelka et al. (2022), *Policy Improvement by Planning with Gumbel* | https://mlanthology.org/iclr/2022/danihelka2022iclr-policy/ | 少量模拟下根动作候选和策略改进 | 当前只当研究对照，不取代Q15正式N/128、不继承理论保证 |
| R6 | Trudeau & Bowling (2023), *Targeted Search Control in AlphaZero for Effective Policy Improvement* / Go-Exploit | https://arxiv.org/abs/2302.12359 | 非初始状态的数据采样与Value状态覆盖 | 本规范没有采纳档案状态重启；只允许实际完整决策帧，无中间状态采集 |
| R7 | Silver & Veness (2010), *Monte-Carlo Planning in Large POMDPs* / POMCP | https://mlanthology.org/neurips/2010/silver2010neurips-montecarlo/ | 信念采样、部分可观测搜索 | 一个固定根世界128次并不等同于标准POMCP的每模拟采样性质，更无博弈均衡保证 |
| R8 | Cowling, Powley & Whitehouse (2012), *Information Set Monte Carlo Tree Search* | https://eprints.whiterose.ac.uk/id/eprint/75048/ | 不完全信息搜索、strategy fusion与信息集状态 | 本方案锁定单世界，明确承认估值偏差风险 |
| R9 | Goodman (2019), *Re-determinizing Information Set MCTS in Hanabi* | https://arxiv.org/abs/1902.06075 | 模拟对手的信息边界泄漏风险 | 方法基于Hanabi；这里只借鉴严格对手视角非干扰审计 |
| R10 | Ross, Gordon & Bagnell (2011), *A Reduction of Imitation Learning and Structured Prediction to No-Regret Online Learning* / DAgger | https://proceedings.mlr.press/v15/ross11a.html | 学生状态上查询A教师的动作/价值 | 固定两轮学生状态补充不继承DAgger的无限迭代理论结论 |
| R11 | Vinyals et al. (2019), *Grandmaster level in StarCraft II using multi-agent reinforcement learning* / AlphaStar | https://www.nature.com/articles/s41586-019-1724-z | 大规模结构化动作条件解码、历史对手与群体稳健性 | 我们不照搬其游戏专用网络或群体训练规模 |

## 哪些属于用户决定而非论文证明

- 单信念世界、每根128次有效模拟、最多32候选、位置18×32、延迟100–3000ms、搜索只在己方节点优化：**用户明确选定的架构与研究预算**。
- FAIR信息边界、同tick时序、预约覆盖与执行合法性：**应由项目引擎契约确认**，不是论文自动赋予的规则。
- 头温度、Replay来源配额、经验卡组先验、候选结构化质量投影、128/16 Full/Fast分配：**待实验配置**。不要把它们写成已有论文验证的皇室战争最优值。
- 旧A来源checkpoint SHA、历史1755与58.45%：**用户提供的项目上下文**，当前未读取模型或重跑评测，不是外部论文结论。
- 搜索与直出同权重双指标晋级：**发布与质量策略**，不是AlphaZero的标准训练步骤。

## 建议引用规则

在实施报告里，每个从文献借鉴的部分应同时报告原始论文、**本项目实现差异**、引擎状态语义、相同总算力对照结果。不把KataGo在围棋的计算效率数字、不把Gumbel在小搜索预算的保证、不把POMCP的理论性质直接套给单世界FAIR搜索。
