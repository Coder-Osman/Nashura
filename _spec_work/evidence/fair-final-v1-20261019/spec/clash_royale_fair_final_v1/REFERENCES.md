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
