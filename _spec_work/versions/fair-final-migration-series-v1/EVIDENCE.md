# 跨轮迁移校验工程证据

日期：2026-10-21。仅工程接口限定范围；不新增总门PASS，不启动正式迁移或训练。

## 已验证

- 复现原相邻校验的缺口：round2复用round0的match/pair ID仍通过原入口相邻检查。
  独立series校验器拒绝上述两个场景，并检查全部历史身份、划分、实际引擎seed。
- 最终tests-final.xml：68 passed、0 failed/error/skipped，76.11秒。
  其中48项本工具专项、20项已有补充轮原生证据测试；不与此前46项初版、
  102项回归、111项历史测试或34个限定子项累加。
- 只读核验既有round0三局和round1两局，5局、4个独立引擎seed、1310行。
  原数据及审计文件mtime/SHA保持不变。数据分布如下：

| 轮次 | 完整局 | train行 | validation行 | test行 |
|---|---:|---:|---:|---:|
| round0 | 3 | 352 | 358 | 334 |
| round1 | 2 | 266 | 0 | 0 |
| 合计 | 5 | 618 | 358 | 334 |

native-prefix-private-plan.json由既有成功attempt审计事后整理，非采集前冻结证据。
excluded_engine_seeds为空，故本次只证明所提供两轮内部互斥，不证明与所有历史实验隔离。
native-prefix-report.json绑定计划、原始回执、成功attempt私有审计及运行依赖SHA；
不重算教师目标、不重新验证原生来源、不提供训练数据合并或父checkpoint恢复证明。

- plan SHA：afe7273dab0c53af9785ea4dcb89e8e7f04a162e0b435126262115f3d3c46477。
- series SHA：a2dccf8f446a91ecf84aad093302d47035bd4254e7acd4f64738714129a97f81。
- validator SHA：72c88798458732f1bdc74e96c0d438554ad2fdb69092cfaa66e6d752ea85f65d。

## 冻结身份与仍开放的工作

原完整局244个生产源码SHA逐项保持一致；history-v2 manifest文件SHA仍为
1f552d1cbcd3384006967242eba80ede42a3d7720edd852f9e33a0f25c02e981。
新代码只在本独立目录，未接入旧入口或调度器；旧补充入口仍只有相邻轮检查。
复用现有manifest、换边与整局数据验收逻辑，新增逻辑限于跨轮索引和只读审计。

以上为原只读校验器范围；本轮独立消费/训练接口及新增证据见下节，
不回写原报告或改变其身份。training_admissible=false，formal_g2_pass=false。

实时搜索快照1792563107.5830624：原完整局worker和29731/29732两引擎存活，
已提交12组，tick466玩家0完成128访问、518.4342秒，玩家1搜索中。
tick212双边跨进程恢复内部比较passed；完整终局、独立收尾、清理尚未产生。
.35中心盘139.168346GiB，距200GiB差60.831654GiB；未降低门槛或清历史。
保留原有每phase六小时上限，不扩大并发、重启或开替代局。

## 本轮：独立全历史消费与补充训练恢复

- 新增series_training.py；SeriesMigrationDataset构造前强制全历史预检，
  原始各轮manifest/receipt与整局划分保留；每批重新验证FAIR/合法性及A目标。
  补充轮可仅含train，但全历史要求train/validation/test非空；私有seed不进模型batch。
- SupplementTrainer采用独立checkpoint schema，从完整固定日程父训练继承模型、
  AdamW moments和累计步数。round1/2分别接原父与补充父恢复器；各轮最多5epoch。
  文件/语义checksum、历史前缀、行为父SHA、实现/数据/损失身份、结构/精度、
  optimizer与步数、全局RNG及sampler RNG/游标均校验；失败恢复原全局RNG。
- training-tests-final.xml：68 passed、0 failed/error/skipped，103.116秒。
  其中37项新专项、31项既有迁移训练回归；它与上文tests-final.xml的68项
  属不同范围，不累加。初版30项报告仅为历史版本，不另计。
- 完整fixture验证round0十二轮、round1五轮、round2转移；0/1/2/3/14/15步恢复，
  下一批、metrics、network、optimizer和全局RNG严格一致。包含21类重封checksum
  篡改拒收、冻结A不变、encoder更新、split隔离、父身份/日程不符与坏回执拒收。
- native-series-training-input-report.json：真实5局1310行只读消费通过，
  train618/validation358/test334；每局第一行、共5行实时重算冻结A监督通过。
  原始文件mtime/SHA与冻结A保持不变；training_updates=0。该结果不证明全部
  1310行目标重算或原生补充梯度更新。原生工程父checkpoint仅1步，未满足
  完整日程前提，因此未用于补充更新；梯度及两轮恢复证据仅来自fixture。

新增身份：

- source A SHA：e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015。
- source network SHA：edf93352f9f512cd1368e989d964ac4d7bb877d200e7247eaacd3aea02ab0953。
- implementation SHA：372a9570d597df1022d295c86d4ca7fb84cf82be3a9b458533bc9cad444b028d。
- probe SHA：66db9b7a8664ed89fc5c86a9f6e9b69584f229348c704fad75e219d3a3406cb6。
- dataset SHA：4eb8d09230d24be4c6efc011f30b4ec8bd21a4f769dd21402f407a683a7b3269。

实现身份绑定新训练模块、原series校验器及原迁移训练器三个文件，不宣称覆盖
全部依赖。series SHA及原validator SHA未变；运行中244生产源码逐项一致，
history-v2 manifest文件身份未变。

仍缺：round2原生、原生连续补充梯度/恢复、正式协调入口接入、外部seed采前登记、
正式1280局、验证选权重/早停父模型凭据、终局WDL校准及非劣实战。本轮不调Loss、
不启动正式迁移或五机放量，不改变G0/G1与G2–G8判定。

最新只读快照1792564228.4273794：原搜索局已提交13组，持久化tick466；tick526
玩家0完成128访问、469.429972秒，玩家1搜索中。worker与双引擎存活，tick212
双边恢复内部见证passed；完整终局、独立收尾与清理待验。中心盘139.168255GiB，
距200GiB差60.831745GiB。未改变phase上限或任务规模。
