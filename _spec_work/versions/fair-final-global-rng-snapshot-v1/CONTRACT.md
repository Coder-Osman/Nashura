# 全局MT快照覆盖：独立新版

本版修复global-rng-audit-v1已证实的2504字节全局MT快照遗漏，范围限于固定
libg SHA110aa2b5…959783、单比赛执行通道。保留旧构建和所有后台冻结任务。
不据此断言固定速猪实际使用该对象，不关闭全部隐藏随机状态或G0/G1。

创建快照时保存全局对象，恢复时在原生反序列化与phase校验后恢复；绑定进程、
libg加载基址、manager与generation。拒绝跨进程、跨manager、跨代次、错误库、
非读写段、多个常驻比赛及native-render模式。反序列化前检查恢复资格。
本版不提供多slot虚拟化，不允许并行比赛共享这块全局随机状态。

state-digest显式采用新scope native-payload+global-mt2504.v1，SHA256输入为
ASCII域fair-final-state-with-global-mt.v1（无NUL）、原生payload长度uint64小端、
原生payload、2504字节全局对象。保留nativeSha256用于对照；不宣称覆盖phase
sidecar或整个进程，历史digest原样保留。摘要前后核验全局状态未被序列化改变。

验收在两个独立新进程、tick90/1200/3600/5400进行扰动/恢复：扰动期间零推进，
每次验证新digest能感知、nativeSha256不变、全部字节恢复、重复恢复及释放正确。
完整有战斗轨迹的续演与实际消费者取证另行报告。原始内存留在私有attempt，
公开发布实现、构建身份和摘要；training_admissible与gates_closed始终false。
