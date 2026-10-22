# FAIR 合法历史与增量信念：工程验收边界

## 输入边界

`fair_final/history.py` 定义独立 `FairHistory`，不是私有 `RecoveryJournal`。
只接收本方开局可见牌序、本方历史决策与 FAIR SHA，以及双方已公开执行的
卡牌、执行 tick 和位置。真实 seed、对手手牌、预约、选择时间及 native digest
不属于此契约；反序列化拒绝额外字段。公开记录不保留原始事件私有 metadata。

记录器按 tick 连续消费本方 FAIR 观察，窗口重复事件去重；自身记录严格按
决策时刻递增。增量历史必须保持旧前缀，并在下一段绑定上一根完整 FAIR SHA；
禁止在旧时间范围追加事件或用同 tick 新历史覆盖旧根。

## 独立条件重建

`fair_final/history_belief.py` 从独立随机 seed 初始化独立引擎，绝不复制
真实引擎快照。己方初始化沿用已经验证的 shuffle 逆置接口；对手只从固定
速猪的公开循环兼容初始排列中均匀抽样。这是工程实验先验，尚不是规范要求的
一般经验卡组先验，也不声称精确贝叶斯后验。

己方历史动作在原决策边界提交，每个自身历史 FAIR SHA 均须吻合。已公开的
对手执行被还原为合法完整 ACT 预约，经现有合法 mask、100–3000ms 延迟、
提前抢占和到期结算路径执行；禁止在最后一个 tick 强制注入公开命令。未公开
历史计划从冻结策略在假想对手自身合法信息下抽样，对已知公开执行前缀做有限
拒绝采样；未来搜索仍须由冻结对手策略正常采样，不把重放未来当作模拟知识。

同 tick 到期先结算：隐藏 ACT 若恰在某个公开事件 tick 到期，会执行，
不能作为被抢占的计划接受。第一次中段验收在 tick 416 暴露此错误，修复为
只有严格晚于已知公开唤醒 tick 的 ACT 才允许以抢占路径条件化；增加回归。

逐 tick 核对公开执行，逐自身决策核对完整帧及 mask；根也须完整 SHA 一致。
无兼容世界、拒绝、截断、提前终局、协议故障或截止期限到达均不输出可训练结果。
仅不兼容条件可有界重试；协议与恢复故障直接停止。

## 快照增量生命周期

`HistoryReplaySession.sync` 从该独立信念引擎自己的已验证根快照恢复，只推进
新增时间区间；包含控制器、双边 tracker、冻结帧、合法掩码和随机状态。
新根通过完整校验后才替换快照；中途失败恢复旧根、保留旧历史及游标。
该会话不是跨进程磁盘快照，也没有更改原生引擎快照协议。

本轮原生验收在双方同时 ready 的中段、后段、加时根，分别验证冷重建后
向下一双边 ready 根的增量推进。独立实际引擎 digest 前后相等；模型参数
及 buffer 前后不变。源代码、日志、冻结合法历史及引擎清理按 SHA 归档。

## 性能解释与未覆盖事项

- 冷重建仍从开局重放，用作初始化或故障恢复；正常已兼容会话可复用根快照。
- 增量 tick 统计不含开局重演；耗时仍含原生恢复、校验与新增推进，不是纯引擎时间。
- 历史当前采用严格不可变 tuple，校验及部分查询仍可能遍历前缀；尚未证明所有
  非搜索开销与局长完全无关，不能宣称后段根耗时问题已整体解决。
- 本轮没有实现新证据使已抽牌序失配时的增量重条件化、仅一方 ready 且对手
  pending 的完整原生覆盖，或将此会话接入每根重新抽样的正式搜索 provider。
- 尚待中后段真实隐藏扰动与固定 RNG 下的 Full128 非干扰、原生 PRNG 隐藏
  状态处理、默认完整视野、整局搜索标签与 actor 持久恢复的联合验收。
- 所有数据均 `training_admissible=false`；没有启动 P2 迁移采集、正式五机
  训练、候选发布或现任模型更新。整体 G0/G1 保持 IN_PROGRESS。

## 复现

从 `firstlight_CR`：

```sh
".venv-live/bin/python" -m pytest native_runner/tests/test_fair_final_*.py native_runner/tests/test_training_tracking.py -q
".venv-live/bin/python" "../_spec_work/evidence/fair-final-v1-20261019/run_history_validation.py" --thresholds 1200 3600 5400 --incremental
".venv-live/bin/python" "../_spec_work/evidence/fair-final-v1-20261019/verify_history_evidence.py"
".venv-live/bin/python" "../_spec_work/evidence/fair-final-v1-20261019/test_history_evidence.py"
```

报告是否通过以最终命令的真实结果为准。本说明不代替通过回执。探针仅占用
`.35` 独立端口 29626/29627，启用前核验可用内存 32GiB、验收盘 5GiB；
内部 1500 秒、外部 1650 秒上限。正式中心 200GiB 磁盘门槛没有因此豁免。
