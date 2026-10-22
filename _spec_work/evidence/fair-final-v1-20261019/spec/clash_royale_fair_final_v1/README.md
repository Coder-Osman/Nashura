# 皇室战争 FAIR AI · 正式训练设计包 v1.0

**日期：** 2026-10-19  
**状态：** 设计冻结候选（Design Freeze Candidate）；**没有核验当前源码、训练服务器或实际checkpoint字节；没有修改代码、没有启动训练**。  
**适用范围：** A → 条件自回归Policy-Value迁移 → FAIR单世界128次搜索 → Full/Fast自博弈 → 连续训练 → 同checkpoint双指标Champion晋级。

## 阅读顺序

1. **[TRAINING_SPEC.md](TRAINING_SPEC.md)** —— 技术协议：目标、模型、规则、搜索、损失、回放、评测、资源、失败处理。
2. **[DECISION_REGISTER.md](DECISION_REGISTER.md)** —— Q1–Q43裁决索引、被撤销的旧规则、仍需只读验证的环境事实。
3. **[DATA_CONTRACTS.md](DATA_CONTRACTS.md)** —— 数据结构、动作/概率语义、日志与版本清单。
4. **[EXPERIMENT_MATRIX.md](EXPERIMENT_MATRIX.md)** —— 控制实验、固定预算对照、优先级与否决条件。
5. **[ACCEPTANCE_GATES.md](ACCEPTANCE_GATES.md)** —— 从来源审计到最终对1755基线的可执行验收表。
6. **[IMPLEMENTATION_PLAN.md](IMPLEMENTATION_PLAN.md)** —— 顺序、依赖与五机资源调度，不包含实际实施。
7. **[RISK_REGISTER.md](RISK_REGISTER.md)** —— 关键逻辑风险和避免伪提升的办法。
8. **[REFERENCES.md](REFERENCES.md)** —— 方法学依据和对应研究的适用范围。
9. **[FULL_DOCUMENT.md](FULL_DOCUMENT.md)** —— 合并后的可直接复制与归档的单一大文档。
10. **[EXPERIMENT_MATRIX.csv](EXPERIMENT_MATRIX.csv)** —— 实验矩阵机器可读索引。

## 审批语义

**LOCKED**：用户明确确认的行为、架构或验收约束，不得在实现中悄悄更改。  
**BASELINE**：为了可复现而保留的初始数值与方法。  
**ABLATION**：待对照决定优劣的实验选项，不等同于已证明提升。  
**VERIFY**：依赖引擎、A checkpoint、实际数据或硬件的事实；当前尚未核验。  
**BLOCKER**：未通过就不可进入下一阶段的正确性门槛。

## 不可误读

- 单世界是**用户选择的预算/复杂度约束**，不是不完全信息游戏已变为完全信息游戏的证明。
- 完整决策边界并不一定等于每隔固定时间的snapshot；未完成边不得获利于截断。
- 自回归条件分解能够表示联合分布，但不承诺任意旧自回归模型可以直接逐参数兼容。
- **正式策略目标第一版仍是 N/128**；候选集探索校正、Gumbel改进目标等**尚未获准替换**。
- 同checkpoint双指标晋级是硬约束；连续Learner、Candidate采集或温度校准不放宽它。
- 搜索时间不推进**游戏时间**，但实测CPU/GPU耗时仍必须计入**训练成本**。

## 状态声明

该包是依据用户提供的现有设计文本和本会话决策生成的**实施前规范**。文件中的方案性参数必须经过只读检查和实验验证；不得将本文本解释为已完成任何系统验收或运行。
