# 单方就绪与对手预约：隔离拒绝诊断

## 适用范围

当前 `public-deployment-own-deadline.v1` 唤醒协议从双方同 tick 就绪开始。
若所有 ACT 均成功：最早 ACT 的公开部署唤醒双方；若双方均 WAIT，
固定 60 tick 的预约同时到期。因此在该协议和这些前提下，
非终局合法轨迹保持双方同步，不应通过手工修改 ready/pending 制造“合法单方根”。
这不是对未来扩展唤醒事件或所有游戏机制的普遍结论。

原生拒绝仅唤醒所属玩家，可产生单方 ready、对手 pending。拒绝局属于隔离数据。
本探针显式跳过一次策略合法性门槛，提交当前 100ms 支付掩码明确排除的 ACT，
保留原生拒绝事件；拒绝后全部续行动作重新走正常合法性校验。
不改 DecisionClock 内部状态、不改支付掩码、不创建训练行、不发布 SearchEpisode。

## 验收内容

- 两个座位分别创建完整 rollout，从无动作开局经合法动作到可复现诊断边界。
- 拒绝后对手 WAIT 的 identity、created_tick、due_tick 和动作保持原样。
- 仅提交 ready 玩家；对手未到期期间无新决策、无额外 RNG 消费。
- 三种续局：错峰 WAIT、提前公开 ACT 抢占、ACT 与对手 WAIT 同 tick 到期。
- 单方 ready 快照每种续局重复两次，核对原生 digest、时钟、FAIR 帧与掩码、
  双方独立执行 RNG 和全部记录轨迹。后续人工置 failed 只用于验证恢复清除失败状态。
- 快照释放、所属引擎父子 PID 退出和端口关闭写入证据。

## 不覆盖

此子项不等于正常训练单方根、信息集搜索、Full128、独立信念重条件化、
原生 PRNG 隐藏扰动或模型强度验收。工程探针资源下限32GiB内存/5GiB磁盘，
不等于正式中心200GiB门槛。整体 G0/G1 继续进行，G2–G8不放行。

## 复现

`run_single_ready_validation.py` 在 .35 独立目录和29630端口运行，
600秒探针内上限、660秒子进程上限、840秒SSH会话上限；不停止其他项目。
`verify_single_ready_evidence.py` 核验双座位×三分支及源码/原始报告/进程身份，
`test_single_ready_evidence.py` 验证13类缺项、篡改和越界声明被拒收。
只有实际报告通过才登记 `G0.SINGLE_READY_REJECTION_DIAGNOSTIC`。
