# G2权重迁移手册：源A → FAIR条件自回归学生 → search-ready

**给Codex的直接执行说明**。本文是迁移协议，不是启动命令授权。新的实现分支通过测试后才在隔离环境部署；原生完整采集和正式1280局必须继续受G0/G1闭合、存储门槛以及用户训练启动授权约束。

## 0. 迁移不是从零学习

原A checkpoint身份：`e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015`，实施时必须重新核验**实际字节与student第128步参数/缓冲区**，不是仅相信文件名。冻结A为`eval()` + 所有参数`requires_grad_(False)`。

新模型的`SemanticEncoder`必须根据源A逐名、逐shape、逐dtype、逐catalog验证后复制；其余新512主干、残差、Mode/Card/Cell/Delay Head及WDL Head根据兼容性复用或明确随机初始化+蒸馏。**现有可验证的直接拷贝接口只有Encoder；Policy/WDL新头不能虚报复制。**

建议的审计示例（需在真实A加载入口，使用真实bundle，不直接提交权重）：

```python
student = ConditionalPolicyValue(bundle=bundle, encoder_config=source.policy.backbone.config)
mapping, inheritance = student.copy_encoder_with_audit(source)
# 将inheritance以只读、含source/学生初始网络及schema哈希的JSON证据保存
# 确认student_parameter_coverage与各head零复制的真实含义
```

报告的`student_parameter_coverage`是复制的**参数元素数比例**，不包括buffer；映射SHA与逐Tensor`torch.equal`确保真实一致。不要以相同形状替代证据；如果给新头提供更强初始化，先做独立函数等价/特征语义实验。

## 1. 旧61档动作适配与合法性

保持 `FullAction = WAIT | ACT(stable_card_id, cell_index∈[0,575], delay_slot∈[1,30])`。60旧有效延迟槽/61旧编码槽中的新前30个100–3000ms是允许段；0ms不得执行，3000ms以上不得落入Policy mask。

适配器为`firstlight_CR/native_runner/training/fair_final/adapter.py`，使用带前缀可行性mask的条件归一化。这**不等同于**旧完整联合分布一次截断后重新归一化，必须分别记录adapter语义、旧/新Mode及Card边际。禁止用`greedy`准确率替代随机直出实战。

## 2. 数据采集计划及分割

先由A适配器生成完整1024局（896 train/64 validation/64 test），再进行两轮学生状态补充（各128局，96/16/16），合计1280局（正式train 1088完整局）；**这不是1280条监督行，而是每局多个真实DecisionBoundary的数据**。

整个原生局属于同一数据分割；双方换边、终局原生证据、采样seed独立于test/confirmation。行为执行动作与教师greedy动作/软分布/WDL分别存字段。旧历史嵌套评测seed由已审计的7,160排除集合单独避开。完整G0/G1未通过不能把工程三局当成正式1024局。

## 3. 教师/学生目标与冻结阶段

`L = λp KL(pA||pθ) + λg NLL(teacher_greedy_action) + λv KL(WDL_A||WDL_θ)`。

- KL必须遍历合法完整支持、按Mode→Card→Cell→Delay的链式概率加权，教师不获得梯度，学生训练取`T=1`，部署头温度另行校准。
- 正式初始：复制Encoder冻结2轮，随后解冻最多10轮；每轮状态补充后的最高5轮属于原冻结BASELINE；有效batch256、FP32、AdamW初始LR1e-4/wd1e-4/clip1.0。
- Encoder冻结时用batch Delay logits，解冻时必须保留已独立验证的student scalar计算图与KL求和顺序，避免AdamW差异超出严格数值容差。
- 允许从128/256局pilot检测拟合与吞吐，但不能将pilot权重当作正式通过。1024+2×128是baseline非硬数据上限：在完整门未达标时按局面覆盖扩充训练，保留独立test不回流。

## 4. 迁移验收与Value重新校准

Teacher fidelity诊断：完整贪心动作≥95%、条件ACT完整动作≥90%、WAIT/ACT≥98%、WDL KL≤0.01、标量Value MAE≤0.02；必须按卡牌、delay、阶段与座位分层。工程12帧、1步尚不满足，不能视为结论。

随后使用新规则的真实**终局**给WDL重新校准，不额外采集中间pending状态；Policy梯度/共享主干如果解冻须监控策略退化。再进行学生冻结温度随机直出 vs A适配器贪心的400局初筛得分≥47%，2000局独立换边配对下界≥47%；测试集与确认seed不得调参、选权重或重复利用。

## 5. Checkpoint、正式初始化与Full128搜索

迁移checkpoint需要记录：源A权重SHA与完整Encoder继承审计、模型架构、adapter、数据manifest、优化器及moments、训练更新数、RNG、采样顺序/游标、不同轮次best权重及验证日志。恢复新版本时不继承旧实现优化器游标，必须从独立初始化开始或有明确迁移兼容性测试。

G2全部通过后生成`initial.pt`：新正式optimizer和空正式Replay/游标，保留源模型/适配器/温度/数据身份。**G3在同一冻结checkpoint上比较128次有效FAIR单世界搜索与无搜索直出是否真实更强**，通过后才允许正式大规模自博弈。所有Policy标签第一版仍是`N/128`，只在完整决策边界Value评估。

## 6. 代码整合顺序（Codex必须执行）

1. 对齐当前`main`与分支SHA，保留现行冻结部署，不更改原生运行局。
2. 先运行冻结参考代码的CUDA六对对照及有效batch256三次跨进程恢复的**已有证据核验**，区分旧版本PASS与新分支NOT_RUN。
3. 在隔离工作树合并本分支，做条件KL数值与梯度对照，计算真实参数继承覆盖率；优先沿用已验证batch-v1数学与训练图规则。
4. 在新的实现身份下重新做原生恢复对照：同一数据、同一teacher、相同采样状态，连续更新 vs 新进程midpoint resume，确认权重/AdamW/RNG/最佳轮次与所有相关指标严格一致，独立生成报告。
5. 运行`tools/run_public_checks.py`和`tools/run_main_engine_checks.py`，源清单校验、gitleaks，发布脱敏证据，再提交和更新Codex回执。
6. 只有全部必须的G0/G1/磁盘门通过且用户明确授权，才进入正式1280局及后续G2/G3；**不得因为这次代码合并而自动放行Gate**。

## 7. 防止误报的证据表

| 项目 | 当前可认定 | 不得冒充 |
|---|---|---|
| 旧三局/1044行严格batch256恢复 | Codex现有旧实现真实原生证据通过 | 新KL整合后恢复已通过 |
| batched-v1六对CUDA | 数值、梯度、AdamW限定对照通过 | 正式1280局训练完成 |
| GitHub公开220项 | 冻结history-v2的CI测试通过 | 当前main及全局原生G0/G1通过 |
| 本分支增量CI | 等新工作流实际结果 | native真实引擎/搜索终局 |
| PUCT等预算 | 代码新增规则与unit等预算测试 | 实战棋力增益已得到证明 |
| Encoder继承 | 可以逐Tensor核验并统计元素数 | 新Policy/WDL Head已直接继承 |
