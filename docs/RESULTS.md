# 阶段成果与证据索引

核对日期：2026-10-22。本文是公开快照说明；完整历史见工程进度与冻结验收清单。

## 已实现及验证的工程范围

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

以上版本目录均位于 `_spec_work/versions/`。测试集合存在重叠，表格数字不相加。

## 当前运行任务的最后已知快照

快照时间戳 1792668454，以及本批独立恢复验收：

- Full128 搜索局已提交 85 组、170 根、21,760 次有效模拟至tick3278；tick3282玩家0搜索中，尚未原生终局。
- 确定性父训练探针已结束，执行1次FP32更新；尚无该更新后的验证选优结论。
- 严格确定性恢复reference与resumed均正常结束，完成归档及独立内容/执行链验收；模型、AdamW、RNG、采样游标/下一批及选优历史严格一致。证据见migration-cache-v1/native-deterministic-resume-verified-20261022.json；仅三局工程范围，不证明终止选优或正式G2。
- 新迁移KL探针通过六对对照，training正常退出、完成1次FP32更新；reference完成初始358行及后续一次358行验证、已保存midpoint，后续resumed及terminal selection待完成。单步报告只有初始验证，不判断更新效果；六对单帧加速比中位数1.376，不代表整轮吞吐。
- .35 中心盘约 137.884GiB，正式中心 200GiB 门保持；备用 .39 仅完成工程样本传输核验。

这些数字只表示当时进度。当前阶段不报告 G0/G1 闭合、正式训练启动或搜索提高实力。

## 历史实验

保留了 `_spec_work/evidence/` 中价值训练、教师采样、学生消费、连续训练及教师评测的 README。它们属于旧协议背景，部分引用的私有原始附件未公开；不代替新版 G2/G3 验收。

## 后续验收顺序

G0/G1 剩余正确性与完整局 → G2 正式迁移及 WDL 校准 → G3 搜索增益 → 后续训练和模型发布门。每阶段证据完成后再更新本页。
