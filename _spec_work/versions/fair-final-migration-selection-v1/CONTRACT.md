# 迁移验证选优、早停与父模型交接工程契约

依据冻结TRAINING_SPEC.md §4.3–4.4和IMPLEMENTATION_PLAN.md P2。
本版本只补轮次接口工程，不启动G2正式1280局，不改变任何验收门槛。
原migration-series-v1、生产入口及完整搜索局244文件身份保持不变。

## 固定规则

- 复用MigrationTrainer、SupplementTrainer、原损失及显式冻结权重；FP32 AdamW不变。
- 启动及每个完整epoch结束时，在全部validation整局上eval/no_grad计算原迁移损失。
  先局内等权再整局等权；不访问test，不把验证行送入优化器。
- 以显式权重的validation总损失选优。patience与min_delta由调用者预先提供并入身份；
  同值或改善不超过min_delta保留更早模型。它们是工程配置，不冒充冻结文档指定数值。
- 初始模型参与选优。round0必须实际完成冻结编码器的2轮，之后最多10轮；
  只在解冻阶段累计连续未改善次数。补充轮最多5轮，从第1轮起累计。
- 终止运行的最新状态与验证选出的最佳状态分别保留；最佳状态包含其对应的
  优化器、全局RNG、采样游标与模型，禁止用最后一步优化器搭配较早最佳模型。
- 独立原子checkpoint保存latest/best原训练器快照、验证历史、配置、数据与实现身份。
  恢复验证日程、选优结果、步数及split身份，并实时重算best的validation指标。
  评估前后恢复模块模式与全局RNG；失败恢复原RNG。
- 单写入者入口连续保存时必须提供上一个文件SHA，核对后原子替换并fsync目录；
  未提供SHA的新建操作保持不可覆盖。该接口不提供多写入者租约或并发CAS保证。
  恢复入口显式核对seed、batch_size及micro_batch_size，参数不一致时拒收。
  实现身份同时绑定选优模块、独立CLI及原跨轮实现身份。
- 下一轮只能从已终止且验证凭据一致的父运行交接；任务behavior SHA绑定父运行文件。
  继承best模型和best优化器步数，不伪造旧训练器COMPLETE或改写其日程。
  新轮仍独立初始化采样顺序，并验证全历史manifest/回执前缀。
- 提供独立选优模型加载接口供新版采集入口使用；旧采集器及正式协调器本轮不变。

## 验收范围

用完整fixture整局验证真实梯度、验证隔离、整局等权、冻结两轮、早停、两轮交接、
最佳优化器继承及中断恢复；篡改凭据与非有限输入须拒收。
本轮结果不代替原生连续补充训练、round2原生采集、正式G2或模型强度证明。
所有产物training_admissible=false、formal_g2_pass=false。

## 独立入口

train_round.py要求--engineering、--source、--private-plan、--data-roots、
--selection-config、--loss-weights、--output、--seed。配置文件分别保存
patience/min_delta及三个原损失权重。--max-updates允许有界工程运行，未到终止边界
只保存恢复状态，不发布可交接父模型。已有output必须提供--resume-sha。
补充轮显式提供--parent、--parent-sha、--parent-config；不读取旧流式日志。
该入口尚未接入正式协调器，不能据此称正式迁移已运行。
