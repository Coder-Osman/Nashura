# 阶段成果与证据索引

核对日期：2026-10-22。本文是公开快照说明；完整历史见工程进度与冻结验收清单。

## 已实现及验证的工程范围

再次fetch远端确认最新主线`da8ca6c12653add5cc4a8667f4809db9625a0ac7`，
清单2000项源SHA与工作区一致。新增active独立归档验收器，外部固定launch/
回执，核验部署、数据、四阶段独立进程和六个checkpoint的真实恢复状态，默认
强制352行、batch256、microbatch1、尾批96。专项21项通过60.40秒，工具/恢复/
验收联合31项通过98.27秒，范围重叠不累加；fixture不作为原生通过。
证据见fair-final-active-migration-v1/verifier-evidence-20261022.json。原生reference
已完成256行和96行两次更新，两个恢复阶段及最终独立验收仍待完成，整体门状态不变。

新增最新主线独立原生验证工具，见`fair-final-active-migration-v1/EVIDENCE.md`。
277文件载荷绑定六个active源码、独立scalar参考与新实现恢复身份；10项CPU
测试最近重跑通过32.96秒。首次CUDA解冻阶段AdamW更新超容差，失败归档287文件
保留：近零梯度差异被优化器放大。已修复教师/学生解冻时均走逐位置路径，冻结仍
合批；learning 11项、migration 26项通过。修复后GPU1六对CUDA损失/梯度/AdamW
比较通过，解冻三对最大梯度差0，快照1792676859已进入reference。冻结/解冻
跨进程恢复及最终独立归档核验待完成，不新增整体门PASS。主工程211项通过36.39秒，
冻结参考220项通过50.25秒，分开记录不累加。单帧配对加速比中位数1.01174，
冻结两个可ACT帧约6.14/6.54倍、解冻约1倍；不代表完整训练吞吐。

| 成果 | 实现与证据 | 证据边界 |
|---|---|---|
| FAIR 信息帧、预约时钟、自回归合法动作、完成边界 MCTS、回放和恢复 | firstlight_CR/native_runner/training/fair_final 及 tests/test_fair_final_* | 局部、内存及原生专项分别记录，整体门开放 |
| 增量信念与局部重条件化 | _spec_work/versions/fair-final-history-v2 | 冻结源码身份可核对；原生附件外置 |
| 局部路径不支持时的一般历史退化 | fair-final-general-fallback-v1/EVIDENCE.md | 73 项相关测试；内存双方 pending 四根 512 次模拟；不证明原生合法可达 |
| 原生合法预约专项 | fair-final-legal-reservations-v1 | 已有 57 分支、114 次重复续演，未观测拒绝；有限覆盖 |
| 迁移数据、消费、选优与断点恢复工具 | fair-final-migration-{series,selection,integration,cache}-v1 | 三局 1,044 行、FP32 batch256跨进程三次更新恢复独立通过；terminal选优、原生round2与正式迁移仍待验 |
| 迁移 KL 合批与教师条件化缓存 | fair-final-migration-batch-v1/EVIDENCE.md | 六对真实 CUDA 损失/梯度/AdamW对照通过；解冻encoder保留原反向图，完整五阶段链路仍待验 |
| 完整搜索局分段续跑与前缀验收 | fair-final-search-continuation-v1/CONTRACT.md | 同一局前缀保存与恢复，不是完整终局 PASS |
| 原生命令与队列静态审计 | fair-final-native-{command,queue}-audit-v1 | 联合 42 项通过；静态 CFG 不证明运行时分支或合法掩码等价 |
| 延迟补牌离线诊断修正 | fair-final-delayed-prior-audit-v1 | 三局 1,044 帧零排除、原生轨迹牌区对照；不证明总体先验校准 |
| 嵌套历史评测种子审计 | fair-final-migration-integration-v1/NESTED_SEED_EVIDENCE.md | 四组3,200条记录新增1,600个排除seed；联合190项通过，5条结局不明确记录不作WDL标签，原始附件外置 |
| 合批父包到采集包的确定性身份交接 | fair-final-migration-integration-v1/collection-runtime-lineage-evidence-20261022.json | 24项通过；实际276源码SHA复核、273父文件保持；仅本地准备，原生round2仍待验 |
| 后续采集预检与有界引擎worker | fair-final-migration-integration-v1/bounded-collection-verification-20261022.json | 43项联合测试通过；278源码采集包、273父文件保持；夹具不替代原生采集与独立整局验收 |
| 后续采集归档独立验收与多轮父身份 | fair-final-migration-integration-v1/selected-collection-verifier-evidence-20261022.json | 47项联合测试通过；重验终止选优父、整局与原始终局、种子来源和所属清理；仅归档内容及fixture范围 |

以上版本目录均位于 `_spec_work/versions/`。测试集合存在重叠，表格数字不相加。

## 2026-10-22 主工程修复分支（已整合、CPU验证通过）

- 已通过fast-forward整合分支 `audit/ci-kl-puct-inheritance-20261022` 最新提交 `8195a5a07b8bf7d9533fc7a3bfbe83b0c5083a99`，保留本机未发布成果，8个新增或修改源码同步回工作区。为当前 `firstlight_CR` 新增独立单测入口/CI；原冻结历史版本公开检查保留。
- 原生迁移批量KL在主工程加入独立批量Delay条件计算与校验。**源独立版本 `fair-final-migration-batch-v1` 的真实CUDA数值与原生恢复证据仍是权威参考**，不可用本分支单测取代；编码器解冻时保留逐位置学生反向图与逐位置KL加法顺序。
- PUCT新增先验首访、旧字典序首访基线；根噪声显式区分 `training/evaluation/ablation_no_noise`，协议SHA随配置变化。
- 权重继承新增基于逐Tensor一致性的覆盖率报告，当前能被证明直接继承的是语义Encoder，新Policy/WDL Head继续蒸馏，不凭相同形状虚报。
- 本机当前主工程211项通过24.12秒，冻结参考版220项通过39.00秒，分别属于CPU工程测试。所拉取提交的两套GitHub CI亦通过：[当前工程](https://github.com/Coder-Osman/Nashura/actions/runs/39779254288)、[冻结参考](https://github.com/Coder-Osman/Nashura/actions/runs/39779254266)。新实现CUDA与batch256原生恢复仍待验，不增加G0/G1 PASS，不启动G2或正式训练。交接与回执见 `agent-handoff/`。
- 归档验收器、五阶段归档核验及旧batch-v1在最新active源码下的兼容性检查75项通过69.40秒，3项因缺少私有冻结部署附件跳过。此项与上面的测试集合存在重叠，不累加；详细身份见 `agent-handoff/INTEGRATION_2026-10-22.json`。

## 当前运行任务的最后已知快照

快照时间戳1792678182至1792678259，以及此前独立恢复验收：

- Full128 搜索局旧段已提交86组后归档；新段恢复后已提交93组、186根、23,808次有效模拟，tick3558玩家1搜索中，尚未原生终局。该任务继续使用原冻结部署。
- 确定性父训练探针已结束，执行1次FP32更新；尚无该更新后的验证选优结论。
- 严格确定性恢复reference与resumed均正常结束，完成归档及独立内容/执行链验收；模型、AdamW、RNG、采样游标/下一批及选优历史严格一致。证据见migration-cache-v1/native-deterministic-resume-verified-20261022.json；仅三局工程范围，不证明终止选优或正式G2。
- 新迁移KL链路benchmark/training/reference/resumed均exit0；reference2061.943秒、resumed1100.468秒，selection进程存活且冻结部署匹配。terminal选优及本链路独立验收待完成；六对单帧加速比中位数1.376，不代表整轮吞吐。
- 最新active链路的benchmark六对通过，reference首个256行冻结更新已完成，初始358行验证479.622秒；冻结/解冻跨进程恢复未完成，旧实现结果不转记为active通过。
- .35 中心盘约137.454GiB，正式中心200GiB门保持；备用 .39 仅完成工程样本传输核验。

这些数字只表示当时进度。当前阶段不报告 G0/G1 闭合、正式训练启动或搜索提高实力。

## 历史实验

保留了 `_spec_work/evidence/` 中价值训练、教师采样、学生消费、连续训练及教师评测的 README。它们属于旧协议背景，部分引用的私有原始附件未公开；不代替新版 G2/G3 验收。

## 后续验收顺序

G0/G1 剩余正确性与完整局 → G2 正式迁移及 WDL 校准 → G3 搜索增益 → 后续训练和模型发布门。每阶段证据完成后再更新本页。
