# SSPI 最小必要历史与重复处理优化

## 本轮实现

- `BattleEnvV1.observe_dual` 通过可覆盖的 `_events_for_dual` 获取事件。`FinalBattleEnv` 直接使用独立 owner 窗口，删除此前随后被丢弃的共享窗口扫描、两次摘要重算以及观测对象重建。
- 同 tick、同窗口的事件元组按 owner 复用；新增可见事件仅使该 owner 的缓存失效，reset 清空。缓存字段纳入既有 sidecar 默认捕获/恢复。窗口内容、顺序和双方隔离规则不变。
- Tracker 在计算事件身份前排除旧 tick 和未来 tick。完成 update 后，去重集合只留当前 tick，仍允许同 tick 后续新事件，拒绝时间倒退；圣水、轮转、已揭示牌及其他当前状态持续保留。
- phase 首轮协议校验按 `(generation, stateEpoch, epochFirstSequence)` 与事件序号复用已验证的完全相同记录。用本机 `pickle.dumps(protocol=5)` 生成类型精确的不可变比较字节，**从不执行 unpickle**；修改内容重新验证，epoch/时间回退不复用。环头、计数、拒绝、连续序号和当前时间校验保持。缓存最多保留当前返回窗口（原生上限 4096 条），不同 resident 槽独立，失败/关闭清除。
- 此缓存是当前 JSON 协议下的过渡优化，会额外占用一个有界 phase 字节窗口；没有减少线上 JSON 字节量，也没有关闭 `validate_json_capture` 或启用 `CR_FAST_ENV`。

## 必要历史不是全部遥测日志

| 消费者 | 需要保留的内容 | 可消费后丢弃的内容 | 恢复条件 |
| --- | --- | --- | --- |
| 对手 Tracker | 当前圣水区间/更新时间、最多八张公开牌的轮转/可用性/已揭示顺序、当前 tick 去重身份 | 已生效的旧出牌事件对象 | Tracker 状态与事件游标一起恢复 |
| 当前战场语义 | 当前实体/目标/弹道/效果；少量仍关联活跃对象的来源与身份；攻击起始、释放、打断摘要 | 已吸收且不再被当前语义引用的旧事件 | 语义摘要与原生快照同一身份 |
| 事件交付 | 各 owner 尚未消费的新公开事件、动作执行/拒绝回执、各环连续序号 | 所有消费者确认后的旧载荷 | 消费游标独立，不以当前隐藏状态重新判定旧事件公开性 |
| MCTS 信念世界 | 开局公开证据、自己的动作/FAIR 摘要、公开出牌 tick/owner/card/x/y；最近有效搜索边界快照 | 不需在每次捕获中重传原始 phase/combat/remaining 全环 | 增量重条件化与一般历史回退当前仍读取短决策/出牌账本，尚未删除 |
| 训练与恢复 | 允许训练的根/派生样本、终局 WDL、动作/RNG/模型/协议身份及 checkpoint | 每个 tick 的完整遥测 JSON | 完整局与隔离失败分开，历史失败证据原样保留 |

并非所有必要状态都只有圣水和轮转：例如王塔激活由 `remaining` 更新，弹道来源/生成组由 `combat` 维护，攻击阶段与打断由 `phase` 摘要支持。它们需要当前事实和新增变化，不要求反复传完整旧环。`remaining` 与 visibility 的原始 oracle-only 字段仍不得进入网络。

## 下一层协议改造

优先补齐兼容的捕获协议：当前对象状态 + 四类事件环增量 + 当前事实摘要 + 独立消费游标。每次请求/响应携带 match/generation/epoch、tick、epochFirst/oldest/transmitFrom/next、overflow/rejected/complete；游标缺口与错误失败隔离，快照恢复同时重置或恢复对应游标。按序确认后无需保留所有旧载荷。没有压缩算法的必要依赖。

当前公开 producer `resident_multimatch.inc` 的 compact 路径写出空 phase 事件窗口（把三个序号置为 phase_next）；decoder 能解析 phase 记录，但 `visibility_runtime`、`remaining_runtime` 仍为 None。此前“compact phase 仍全量”的笼统判断不适用于这个 producer 版本。已部署二进制应另以身份和实际响应核对；**空窗口不等于已证明语义等价**，因此本轮未直接切换客户端，也未重启/替换 AVD probe。

后续验收在独立槽逐捕获边界比较：双方 FAIR SHA、当前攻击/王塔/因果语义、合法动作、补牌状态、新事件与回执、动作随机流、终局以及 MCTS 恢复/访问分布。通过之后才让正常采集仅传增量。旧大型失败文件保留为诊断证据；新运行默认保留必要样本和有界错误现场，不把历史文件清理混入协议优化。

## 验证边界

本轮测量与测试摘要见 `RESULTS.md`。局部缓存和无搜索同轨迹对照不代表完成新版混合采集、训练更新、MCTS 性能验证或模型强度评估。

复现输入：基线 `1d7b021`；新版以证据 JSON 中逐文件 SHA 与发布提交为准；Python 使用既有 Mac 3.13 环境、CPU 单线程、固定模型 seed 20261103、固定速猪、`run_id=sspi-host-history-20261102`、`game_id=BENCH-0001`、`random_stream_id=sspi-overhead-benchmark-pair-0001`。在已检查空闲的 resident 槽调用现有 `collect_episode(..., episode_kind="evaluation", search_probability=0.0, temporal_augmentation=False, transport="event-json")`；两版传入同一槽、端口和经核对的 probe 身份。每轮从同一 MatchConfig 重新开始；不复用上轮终局状态。槽 0/1 已按用户后续明确指令释放，槽 2 用于诊断。

所有原始动作、隔离报告与日志仅保留本机独立目录，公开摘要保留动作 SHA、模型/引擎/源码 SHA 和阶段计时。原生执行拒绝不修饰为平局，带 sys.settrace 的诊断计时不与普通计时混合。发布工作树为本轮实现源；外部旧部署源码保留，避免将其旧版本重新导出覆盖本实现。
