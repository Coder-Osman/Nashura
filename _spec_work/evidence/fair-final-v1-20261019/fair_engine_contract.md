# FAIR final v1：独立新版时序实现与验收

用户已裁决：独立新版按文档修正；旧运行时保留用于历史复现。

## 实现范围

新版代码位于 `firstlight_CR/native_runner/training/fair_final/`。没有替换旧
`fair_search`、旧 PUCT runtime、旧 checkpoint、回放或优化器。
原生探针使用 `.35:/home/zhichun/fair-final-v1-20261019-p1` 和独立端口 29619。
原生二进制复用已核验 P18，SHA256：
`0ed1b03a9d0035794fb1cdeacde7cfe4757bf27ebe80f1f0d28c7be7ba79f807`。
规则清单仍校验该身份，没有关闭 attestation。

## 可执行阶段时序

| 阶段 | 操作 | 不变量 |
|---|---|---|
| 决策 tick t | 分别冻结双方 FAIR 信息，原子提交所有 ready 玩家动作 | 一方看不到另一方未公开选择 |
| 预约 | WAIT 期限 t+60；ACT 期限 t+2×delay_slot | delay_slot 只允许 1..30 |
| 普通推进 | 一次推进一 tick | 推理墙钟不推进比赛 |
| 到期前一 tick | 仅给下一 tick 到期的 ACT 注入原生队列 | 更早事件发生前没有不可撤回队列命令 |
| 原生执行 | 原生检查选择、资源及位置并推进至到期状态 | 队列回执不是执行证明 |
| 到期状态 | 复用原生手牌槽变更核验及公开 card_play 交叉验证 | executed/rejected 必须有结果 |
| 公开事件 | 唤醒双方，并使尚未到期预约失效 | 到期预约先结算，不被同 tick 事件覆盖 |
| 私有拒绝/自身期限 | 仅唤醒所属玩家 | 不以对手私有期限唤醒自己 |
| 冻结观察 | 到期/抢占结果确定后冻结 | 中间 pending 状态不作训练行 |
| 异常 | 标记 runtime failed，拒绝继续；可显式恢复已验证快照 | 不在半入队状态盲目重试 |

本版唤醒协议为 `public-deployment-own-deadline.v1`，沿用旧卡牌公开部署事件，
没有擅自扩大为所有战斗事件唤醒。终局终止剩余预约且不开放新的训练决策。

新版完整局要求原生 `finalized=true` 后才报告终局。gameplay关闭与结果确认是两个
边界：前者终止剩余预约并停止策略决策，后者才允许发布整局。确认期间无新训练行。
首次正常局末次决策tick5998，最终tick6127；全WAIT对照末次决策tick5970，最终tick6068。
接收层要求 `worldResult` 与 `worldResultRaw`、winner、双方标签一致；
平局还要求已finalized、原生结果-1与双方公开冠数相等。未知结果码、截断或看门狗
超时均不转换为平局。全WAIT真实原生探针已验证该平局编码。

## 原生支付边界的实测修正

原生 `queue_hand_action_at(execute_tick=t)` 的消费边界是 t−1。
在 tick 168 只有 39,904 原始圣水单位、成本为 40,000 时，指定 tick 169 的命令被拒绝；
tick 169 推进产生的圣水不供这次命令支付。此发现没有改变已选择动作的到期时间。

新版前缀合法性按真实整数增长率 178/357/537 和消费边界计算：
决策 tick 到 `due_tick−1` 累积的资源必须足够。此临界状态下 100ms 被屏蔽，
200ms 仍合法且原生执行成功。旧 61 档到新 30 档保持 100ms 档位，无索引偏移。
这是原生合法性适配，不是增加最低延迟，也不是将全部当前不可支付牌剪掉。

## 神经与概率实现

- 条件策略为 Mode → 稳定 Card ID → Position → Delay。
- `log_prob` 返回温度 1 的训练概率；`behavior_log_prob` 与 `sample` 返回部署温度分布。
- A 适配器 `source-a-old61-new30-prefix-normalized-native-payment.v1` 采用前缀条件归一化；
  不声称等于旧完整联合空间全局截断。
- 神经卡牌特征按 frame catalog ID 显式重排，不假设合法掩码和张量行顺序相同。
- Full128 策略损失和全部行 WDL 损失分别归一化；Fast 没有 Policy target。
- 新模型的编码器复制先验证配置、catalog、参数形状和 dtype；新头不按形状盲目复制。

## 当前验收边界

单元测试通过不代表完整 G0/G1 通过。已运行的原生探针覆盖同 tick 双方到期、
提前抢占、支付临界、运行时快照恢复，以及 A 模型真实 FAIR 帧上的短轨迹采样。
短轨迹明确标记 `complete_episode=false`、`training_admissible=false`。

新增恢复验证同时保存tensorizer/tracker、冻结帧及合法性、双方独立执行RNG。
原生分支重演要求输入哈希、动作、控制器状态和引擎digest完全一致；
GPU FP32数值另行比较（atol=rtol=1e-6），报告实际最大绝对误差。
此前失败已定位为相同输入下的微小GPU数值差异，保留原始失败attempt，未放宽
FAIR输入、动作或引擎状态的一致性要求。该检查不证明128搜索在GPU下严格确定性。

新增FAIR探针直接在原生引擎交换真实对手的手牌与抽牌队列，两边位置分别检查；
CPU模型输入、合法性、候选和采样结果要求完全一致，审计区验证真隐藏状态确实改变。
该探针仅覆盖开局牌序；不等同于整局、隐藏计划、私有seed及模拟对手全链路验收。

完整局验收记录器与正式训练数据分开：`FairFinalAcceptanceEpisode/v1`，
`search_mode=NONE`，`policy_target=null`，`training_admissible=false`。
逐帧保存FAIR输入和合法掩码，记录所有合法预约，包括正常抢占；
拒绝或缺少最终执行证明则整局隔离。原生私有seed和末帧在独立audit目录。
完成回执最后原子发布；保存压缩文件与语义双重哈希，并在本地重新读回校验。
这只证明完整局记录路径，不代表teacher软目标、正式Replay或学习器已就绪。

尚待完成：全局隐藏状态扰动非干扰、正式数据/checkpoint契约、跨进程持久恢复、
单世界搜索与128有效访问、迁移数据/训练入口和门槛。
目前未启动1024局迁移训练、正式128搜索生产、五机放量或 Champion 发布。
