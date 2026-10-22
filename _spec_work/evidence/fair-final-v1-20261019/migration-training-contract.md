# 迁移损失与更新器工程接线

本证据对应 TRAINING_SPEC.md §4.2–4.4 的工程子项，`training_admissible=false`。
尚未启动 G2 的1024局或两轮128局迁移采集、正式拟合、WDL终局校准、
400/2000局非劣评估，也未导出正式 `initial.pt`。

## 实现

- `native_runner/training/fair_migration.py`：冻结A适配器产生完整合法条件
  分布和WDL教师分布，接入新的条件联合策略模型。
- 策略KL逐合法前缀计算并按教师到达概率加权，等于完整合法联合分布KL；
  贪心动作NLL使用学生原始完整动作概率，不在候选集内重归一化。
- WDL采用 `KL(WDL_A || WDL_student)`。三项权重必须显式给出，有限、
  非负且至少一项为正；测试权重只是工程输入，不是验证集选出的正式权重。
- 教师必须完整冻结且处于eval，温度固定1；教师分布与WDL均detach。
  学生部署温度不改变训练原始log_prob；贪心诊断不消耗全局执行RNG。
- `native_runner/training/fair_migration_trainer.py`：独立的有界更新器。
  前2轮冻结编码器并保持其eval；随后至多10轮解冻。使用FP32 AdamW，
  LR=1e-4、weight decay=1e-4、clip=1；默认有效batch=256，微批=1。
  按微批真实行数加权累积梯度，只在有效批末尾裁剪和更新一次，避免同时保留
  256行完整条件分布的计算图；微批大小与检查点绑定。
- 每轮对固定train行作一次无放回洗牌，最后不足一批按真实行数更新并记录，
  不补伪数据。调用方须传回当前游标的精确行ID；validation/test行不进入更新。
  三个划分的行ID必须互斥。完整局身份加载现由独立MigrationDataset核验，
  `update_dataset` 对接可信行游标；原生采集入口三局工程验收已通过，见
  migration-native-contract.md。
- `FairFinalMigrationTrainer/v1` 是独立检查点，不接受正式Replay/PPO状态。
  保存来源、适配器、数据身份、完整划分、教师参数摘要、模型架构及状态、
  AdamW、Python/NumPy/torch RNG、私有洗牌RNG和训练游标；原子且不可变写入。
- 恢复重建轮次、行顺序及样本账本，核对来源、划分、权重、架构和优化器；
  失败恢复保留调用前RNG。更新出现异常后隔离该实例，不继续更新或发布检查点。

## 验证

`p1-migration-loss-tests.xml`：接入数据加载后重跑64项通过（36.03秒），零失败、零跳过：

- 损失模块26项：完整小动作空间穷举参考、分项权重、教师无梯度、非法监督
  拒收、WAIT-only、训练/部署温度区分，以及真实模型冻结/解冻梯度。
- 更新器31项：真实fixture模型完成2+10轮、所有train行每轮恰好覆盖一次，
  六个更新位置的断点恢复（包含最后不足批、编码器解冻边界、训练完成）
  后续指标、参数、优化器、随机状态逐项相同；来源/划分不符和损坏状态拒收；
  冻结与解冻阶段的微批梯度累积和整批损失/参数更新在FP32容差内一致。
  跨微批实现比较从相同参数与AdamW状态出发：梯度atol=1e-7/rtol=2e-5，
  参数atol=1e-5/rtol=2e-5。批量矩阵乘的舍入差异可经AdamW近零梯度的
  归一化放大，因此不要求跨微批逐位相同；固定微批配置的断点恢复仍严格相同。
- 相邻学习模块7项：完整联合KL、Full/Fast分母、动作条件依赖及延迟映射。

小数据拟合只用独立小空间参数，AdamW LR=0.03、300步，纯策略KL+WDL KL
均降至初始值的1/100以下且小于0.001；这不是正式超参数或真实局面保真结果。
真实模型fixture仅验证训练接线、阶段切换及恢复，不据此宣称实战能力改善。

复现：

```sh
"firstlight_CR/.venv-live/bin/python" -m pytest \
  "firstlight_CR/native_runner/tests/test_fair_migration.py" \
  "firstlight_CR/native_runner/tests/test_fair_migration_trainer.py" \
  "firstlight_CR/native_runner/tests/test_fair_final_learning.py" \
  -q --junitxml="_spec_work/evidence/fair-final-v1-20261019/p1-migration-loss-tests.xml"
```

## 边界与后续

完整局记录/存储、帧与行ID的可信加载和按局划分已完成工程接线，47项专项
测试通过；见migration-data-contract.md。原生完整局三局与真实train帧两步
更新/精确恢复现已工程通过；这不是正式拟合。尚缺验证集分层诊断/权重选择
与早停调度、两轮状态补充（各最多5轮）的外部协调器、
真实终局WDL校准及G2非劣门槛。当前更新器只实现第一批2+10轮上限，
不能拿它的工程测试替代上述验收。

三个迁移生产模块放在 `fair_final/` 的兄弟位置，保持原生Full128证据
所冻结的 `fair_final/*.py` 身份不变。总门槛另收录它们的源码和测试摘要。
