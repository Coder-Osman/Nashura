# P1：正式数据契约与持久恢复子项

## 本次范围

新增实现均位于 `firstlight_CR/native_runner/training/fair_final/`，不修改旧PPO、
`fair_search`、旧`az_joint`数据类型及优化器。规格依据为设计包DATA_CONTRACTS第3、5–9节。

本次提供正式Schema实现及隔离测试，不启动真实Full128生产。测试中的搜索统计为
明确的合成契约夹具，只验证拒收条件，不作为真实搜索正确性或教师质量证据。

## 数据与存储

- `contracts.AZJointEpisode`：`AZJointEpisode/v1`，实现身份 `fair-final-data.v1`。
  严格字段白名单、8张稳定卡牌、FP32单帧、精确合法掩码和语义SHA。
- 逐玩家核验真实决策顺序及上一预约结算时间；同tick按owner排序；
  gameplay结束tick与原生finalized tick分别记录，后者确认之前不发布。
- `FULL`：恰好128访问，候选合法去重、模式/卡牌质量守恒，监督逐项对应本行根；
  `FAST`：恰好16访问、策略标签为空；`NONE`：没有搜索结果或策略标签。
  以上是数据入口检查，不代表搜索器已经实现。
- 搜索值带根玩家视角；未完成边回传计数必须为0；WAIT先验不参与ACT根噪声。
- 只接受原生finalized终局，按玩家派生WDL，保留正常抢占，拒收拒绝和未确认。
- dataset manifest先冻结整局split；换边同seed对亦保持同一split。
  数据身份包含引擎、FAIR、动作、模型、控制器及搜索协议SHA。
- RNG只存流身份哈希，真实seed及隐藏世界内容不在训练Schema。
  固定tensor类型及哈希检查不替代完整FAIR来源审计，后者仍有待完成子项。

存储API：

1. `EpisodeStore(root, manifest=...)` 固定不可变清单。
2. `store.stage(episode)` 校验完整终局并写入压缩、校验过的恢复对象。
3. `store.publish(semantic_sha)` 最后原子发布match完成回执。
4. 重启后使用同一清单与semantic SHA重复publish；同一match只接收第一次有效完成。
5. `store.load(match_id)` 重新检查文件、语义、数据身份及split。

完成标记发布前进程退出：已落盘终局对象仍可恢复；半写临时文件不被发现为完成局。
本轮验证包含真实独立Python进程`os._exit(73)`故障注入和两个进程争抢同一完成标记。
这不是原生进行中比赛的持久快照恢复，也不是文件系统断电可靠性证明。

## 学习器与候选

- `FrozenReplayCursor`：本轮固定数据集先按整局均衡、局内打乱轮转；
  保存逐行覆盖、排列、游标、采样RNG、已消费数和剩余预算。
  validation/test/confirmation拒入优化；不实现近期窗口与来源分层扩容策略。
- `checkpoint.save_checkpoint`：不可变 `AZJointCheckpoint/v1` LEARNER；
  权重/缓冲区、AdamW动量、参数顺序、requires_grad、逐模块train/eval状态全部保存。
  同时保存Python/NumPy/Torch CPU/CUDA RNG、数据游标、预算及协议绑定。
- 只在完成optimizer更新且梯度清空后保存，不伪装支持梯度累积到一半的恢复。
- `resume_checkpoint`：先检查文件SHA、各组件SHA、架构/catalog、数据及预算；
  拒绝错误优化器形状、清空的动量、协议漂移、CUDA拓扑变化和旧Schema。
  恢复失败还原调用前全局RNG，避免失败路径污染下一次尝试。
- `export_candidate`：导出CANDIDATE保留同一network SHA，不携带优化器与回放游标，
  不赋予Champion身份，不更新线上指针。固定帧策略概率/WDL导出前后严格一致。

## 已测证据与限制

- `p1-persistence-tests.xml`：完整本地回归；新增专项含格式负例、分割隔离、预算、
  长短局均衡、发布恢复、并发去重和冻结编码器状态恢复。
- `run_persistence_probe.py`：保存独立测试数据与checkpoint，执行1次更新后落盘；
  原进程继续2次，与新Python进程恢复后2次比较。
- 两条路径逐步loss/随机数/采样行及最终network、optimizer、replay、RNG SHA完全相同。
  证据在 `persistence-report.json`，内含源码和输出文件SHA；Gate脚本拒绝过期证据。
- 验收为CPU FP32、fixture batch=2。正式默认batch256；这里不声称CUDA精确一致、
  实际教师标签可学习、跨进程原生引擎恢复或策略胜率提升。

## 下一块

正式迁移数据还需teacher完整条件soft目标与teacher greedy/WDL；未创建替代硬标签。
需继续整局FAIR非干扰、进行中actor持久恢复/同seed重跑及有界单世界Full128搜索。
G0/G1整体继续IN_PROGRESS，G2及后续仍未放行；本次没有远端采集或正式训练任务。
