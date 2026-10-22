# 独立迁移完整局数据链路

依据 TRAINING_SPEC.md §4.2–4.4 与 DATA_CONTRACTS.md §4。
本项含工程fixture与三局原生链路验证，`training_admissible=false`；
G0/G1整体仍进行中。原生证据见migration-native-contract.md。

## 数据身份与信息边界

`native_runner/training/fair_migration_data.py` 提供独立的
`JointMigrationEpisode/v1`、`MigrationRecorder`、`MigrationStore` 和
`MigrationDataset`。不接受 SearchEpisode、正式 AZJointEpisode 或 PPO 数据。

采集前冻结 manifest：来源A文件SHA、完整参数和buffers语义SHA、适配器SHA、
引擎/动作/FAIR/观察协议SHA、稳定卡组、轮次、任务及双方行为模型身份。
完整局预先分到train/validation/test；同一换边种子对也不得跨划分。
真实比赛种子与对手私有状态不属于学习数据契约。

第一批双方A适配器贪心；两轮补充中一方学生、一方A。实际执行动作与冻结A
教师动作独立保存，支持在学生偏离状态上监督。每行记录精确FP32 FAIR语义帧、
bool合法mask、玩家/tick、预约身份、教师WDL及真实终局标签；正常抢占保留。

`teacher_conditionals_ref` 绑定冻结A完整网络、适配器和该行FAIR摘要。
它是可复算的完整软分布引用，避免逐帧展开百万级联合动作表。训练时由
`migration_batch_loss` 在精确帧与合法mask上重新计算完整合法条件分布KL；
教师argmax仅作为独立NLL辅助目标，不代替完整分布监督。加载时另重算教师
贪心与WDL，拒绝与记录不符的目标。所有教师参数冻结并保持eval/FP32。

## 完整性、存储与消费

只接收原生完整终局。核验双方轨迹连续、所有预约结算及真实终局证据，拒绝
截断、协议拒绝、未确认和未finalize。标签按行所属玩家转换；真实平局单列。
压缩数据先写入，最后原子发布完成回执；未完成回执不被索引，中断可重试。
同任务仅首个完整成功结果被接收，重复回执幂等；每任务最多3个attempt。

读取逐项核对文件SHA、语义SHA、完整局契约、回执计数和隔离标记。
索引要求所有预注册局完整，数据身份绑定manifest与实际接收回执摘要。
索引后替换有效数据也会被拒收；相同manifest下不同结果产生不同训练身份。

`MigrationTrainer.update_dataset` 校验数据、三划分与教师网络身份后，按持久
训练游标加载精确行。默认batch只读train；显式validation/test读取仅用于诊断。
微批更新、AdamW及断点恢复沿用已验证的独立迁移更新器。

## 验证及范围

`p1-migration-data-tests.xml`：47项通过，零失败/跳过。包括双方胜负/平局、
正常抢占、完整局划分、实际动作与教师分离、同tick提交约束、RNG保持、
损坏数据/文件/回执/教师拒收、幂等发布、发布中断恢复，以及真实fixture模型
通过数据集更新并精确恢复后续训练。身份拒收时参数、优化器、游标及RNG不变。

原生采集入口现已完成三局工程验收：train/validation/test分别352/358/334行，
所有原生终局、双边标签、正常抢占及数据身份复核通过；在真实train帧上完成
两步FP32 batch=1更新，并验证连续与恢复更新严格一致。此规模只验收接线。

本模块不单独批准正式数量：1024局896/64/64与两轮128局96/16/16仍由后续
原生采集协调器强制校验。尚缺验证集权重选择、补充轮调度、WDL终局校准
或400/2000局非劣实战。工程测试不代表策略变强。

```sh
"firstlight_CR/.venv-live/bin/python" -m pytest \
  "firstlight_CR/native_runner/tests/test_fair_migration_data.py" -q \
  --junitxml="_spec_work/evidence/fair-final-v1-20261019/p1-migration-data-tests.xml"
```
