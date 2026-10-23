# 2026-10-23 静态限定证据

基于 fetch 全部远端分支确认的最新主线 `11bc2ac` 继续，运行部署未修改。
固定库 SHA256 为 `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。

已完成 14 段字节摘要、18 处 MT 初始化直接引用及唯一 EH 函数归属核验。
手牌分支的栈临时生成器，初始化后正常控制流覆盖 225 个指令/状态组合，
唯一正常返回 `0xf3f7f4` 前经过 `0xf3f7c8` clear；两处 generate 与 clear
均引用 sp+0x40。seed 来源是 manager 随机输出加 identity 低字，而非将此
MT 初始化函数混同于读取时间缓存的 xorshift seed setter。

另独立核对 ct1548/subtype55 的两级表分派和 vtable：旧 tick-seeded 路径属于
该分支，而非手牌初始化。其固定卡组可达性仍待证明，另外 16 处引用保留 unresolved。

新专项含正常分支、循环、绕过清理、间接流、异常、清理后生成、重复清理、
表索引边界，以及真实固定库重算和六类内存中篡改拒收。与 seed 生命周期
既有 50 项联合共 74 项通过、零跳过，3.79 秒；不相加为另一套独立验收。
本次静态结果不新增原生轨迹、隐藏非干扰、先验校准或整体门 PASS。

可复核身份：

| 文件 | SHA256 |
|---|---|
| shuffle_audit.py | 20f456d03c62520e9afa8e1b17de102580bd3b904a7829b3678a3bdb6c6c364e |
| audit_shuffle.py | 48307d47922e7fa8246be6d4f82271a550688a90446a38c299c12e05fc3661a5 |
| test_shuffle.py | 6a8014cc7f96e07f9b18330ff8b9de765611a1ed8aac7fd7586adbc6c571ca88 |
| static-report.json | 4352ed9890cc99f0067b307c35f4956f75ada50cfec8a73680d1802b83daf60f |
| tests-joint.xml | 7ba64e85a1d0a170c5876706de5f98040c9dd4eb1983e71d9a87be93a6064dff |

后台快照 `1792728322.018626`：Full128 已提交 114 组/228 根/29,184 有效模拟，
最后提交 tick4170，下一根 tick4230 玩家0计算中；worker、双引擎在线，
无失败、未终局。中心盘可用 206.29105GiB 为瞬时读数，不代替两代容量验收。
active 独立重试仍存活，日志已完成第6步，尚无 completion 或 failure；
status.phase=selection 为旧 helper 通用命名，并非真正 terminal 选优。

整体 G0/G1 及 34/43 限定登记保持开放，G2–G8、正式训练及五机放量未启动。
后续优先完整搜索/恢复终局、一般合法边界、随机覆盖与真实迁移轮次交接。
