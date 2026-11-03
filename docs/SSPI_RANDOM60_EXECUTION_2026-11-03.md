# 随机初始化60%目标：执行与恢复

用户已批准执行 `SSPI_RANDOM_TO_60_EXPERIMENT_2026-11-03.md`。本文件记录执行实现；训练结果须以独立 run 的完整 episode、generation、checkpoint 与评估记录为准。

## 固定入口

`python -m native_runner.training.sspi.random60 --run-dir RUN_DIR --initialize-only` 只创建全新初始化及运行身份；随后使用相同代码和环境执行不带 `--initialize-only` 的命令。运行目录必须独立，原历史实验不迁移。

入口锁定 seed20261103、CPU单线程、确定性算子、5%×128、原模型及动作协议。G1/G2采集4+2搜索局，其后8+4；每代使用全部当代有效根最多两遍、batch上限128/microbatch4、AdamW lr1e-5，再追加32+16直出价值局及head-only lr1e-4更新。G1/G2/G4/G8各64个换边种子对筛查；候选锁定后进行独立域最多1500对正式评估。无超参数网格或历史数据回放。

## 安全恢复边界

- 所有完整 episode 恢复时核对模型、种子、原生引擎、probe、collector源码、终局、动作及tensor校验和。失败或截断保留目录并暂停，不换种子补采。
- 每个联合batch先保存尝试身份，再执行原有稳定性门。接受或拒绝后保存含完整曝光计数、模型及AdamW的原子checkpoint。拒绝只回滚该批，保留此前接受的更新并结束联合阶段。
- 如发现尝试身份已落盘而对应checkpoint未提交，暂停检查，不默默重复训练。完整checkpoint先于可读generation JSON提交；中断后可从checkpoint重建该JSON。
- 代末价值阶段复用现有阶段锁、最佳模型与优化器恢复及数据校验。共享主干和策略函数SHA保持，固定G1验证状态上的CPU模式概率逐位核对。WAIT统计分开报告有合法PLAY的状态和强制WAIT状态；模式总变差明确不称为完整联合策略KL。
- 评估锁定候选SHA、theta0、协议、源码和seed域；半对故障暂停，已完成局重新读取而不重打。正式E统计只累计完整换边对；至少64对，log-E达到log(20)才通过，最多1500对。恢复重新验证完整账本且不重复累计。
- 同一run及actual端口使用进程文件锁；重复启动被拒绝且不覆盖活跃进程状态。连续两代无接受的策略更新时，保存成果并进入needs_diagnosis。

## 结果与局限

工程测试使用合成FAIR状态及合成episode验证数值、控制流和恢复，不能替代原生训练及60%胜率验收。原生首局及后续运行进度另登记到结果索引。训练checkpoint、episode及probe二进制只保存在本机，公开仓库发布代码和去除私有内容的证据摘要。
