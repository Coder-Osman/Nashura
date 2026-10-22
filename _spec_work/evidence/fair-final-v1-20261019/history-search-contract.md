# 合法历史信念恢复与搜索接线：工程边界

## 接口和信息边界

`history_provider.py` 仅接受 `FairHistory` 和该根的 `DecisionInformation`。
根owner、tick、完整语义帧与合法mask SHA均须匹配；历史必须延续上次已核验前缀。
禁止从实际比赛、RecoveryJournal、实际seed或对手真实私有数据恢复搜索世界。
历史输入仍使用固定速猪工程先验，不等于一般经验卡组后验。

## 两条路径不得混淆

1. `RecoveringHistoryCursor`：已有独立世界沿公开历史增量同步。仅
   `IncompatibleHistory` 触发有界冷重建；deadline、协议或快照恢复错误直接失败。
   冷重建之前释放旧快照和transport，失败不提交新历史游标。每次记录cold、
   incremental或rebuild及耗时。失败尝试再次执行使用同一历史派生随机种子。
2. `HistorySearchProvider`：满足搜索接口 `build(information, seed=...)`。
   每次build建立一个新的独立条件世界；根内128次模拟复用该世界快照，
   搜索结束释放world snapshot、session snapshot和transport。一个provider
   同时仅有一个活动lease；构建失败和搜索异常同样清理。

游标沿用旧世界不等于下一根重新抽样。因此目前搜索入口刻意不沿用
`RecoveringHistoryCursor` 的跨根世界。每根独立重建仍随历史长度增长，
本次接线不是“后段非搜索成本已恒定”的性能交付。后续需要经原生核验的
根局部重条件化或等价的独立抽样方案，禁止以速度为由改变锁定采样语义。

## 模型与清理证据

world manifest绑定完整根SHA、合法历史SHA、sampler SHA、独立seed commitment、
冻结对手controller和checkpoint；明确标记 `training_admissible=false`。
源码SHA与部署清单独立保存，搜索结束核对实际引擎digest未改变。
此模块不修改旧PPO、旧FAIR搜索和之前已验收的新运行时。

## 有界原生验收范围

`run_history_search_validation.py` 在.35独立端口29628/29629运行：

- 实际轨迹连续12根验证增量同步与自然出现的信念失配重建；
- tick≥1200时，两座位分别执行实际隐藏牌序扰动前后Full128，共4根；
- 固定搜索RNG，比较完整候选、N/Q、实际采样动作、policy target和world commitment；
- 对照搜索不改变实际比赛，不更新源A参数或buffers；
- 结束检查所属父子PID退出、两个端口无监听；仅清理本次创建的进程。

工程视野为2决策组/60ticks，扩展预算6组/60ticks；不是默认6组/200ticks。
进程内1740秒、外层1800秒上限；资源前置为32GiB内存、5GiB工程探针磁盘，
不改变正式训练中心200GiB磁盘门槛。

原生双方仅一方ready且对手pending的根、后段/加时Full128、原生PRNG扰动、
一般经验先验、完整局搜索质量与恒定成本重条件化仍是单独未覆盖项。
本地双方pending根恢复测试不是这些原生门槛的替代品。

## 验收命令

- `run_history_search_validation.py`：收集独立attempt和完整原始证据。
- `verify_history_search_evidence.py`：校验源码、原始历史、搜索预算、对照一致和清理。
- `test_history_search_evidence.py`：缺座位、假访问数、改根、隐藏输入、旧世界复用、
  隐藏状态未变、改源码、改artifact、未清理、假实际状态不变、缺恢复记录等负例。

所有子项报告只有在证据校验通过后写PASS；G0/G1整体继续IN_PROGRESS，
没有启动正式采集、迁移训练或Champion发布。
