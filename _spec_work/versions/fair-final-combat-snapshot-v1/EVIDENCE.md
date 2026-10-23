# 有战斗快照续演的限定证据

日期：2026-10-23。状态：`verified_limited_scope`。
本实验是G0/G1的补充证据，不是完整搜索终局、正式迁移或训练准入。

## 原生结果

在`.35`使用global-rng-snapshot-v1已验收的单通道引擎，启动两个独立新原生
进程。固定同一速猪比赛seed及启发式动作，两进程分别推进1110tick，从90到1200。
tick110/600/1200共六个根均有双方未结算预约；每根执行两条各60tick战斗分支，
逐tick结果完全相同，重复恢复后的完整根、全局MT字节及预约身份一致，句柄释放。

每进程主轨迹玩家0/1分别实际执行14/13个ACT，正常抢占取消5次。
纳入主轨迹及分支的1469帧含非塔实体，1441帧含公开HP损失；这不是空WAIT测试。
决策依据各自冻结FAIR掩码，双方同tick选择后统一提交；不读取隐藏状态选动作。
验收重建全部预约历史，检查动作合法性、due结算、WAIT到期及取消记录，
并比较native+全局MT摘要、native摘要、FAIR摘要、执行RNG、公开实体及血量。

两进程分别耗时122.793/127.903秒，均在注册的300秒探针上限内。
21份原生归档的文件集合、字节SHA、源码/配置/父构建身份和清理回执独立核验通过。
进程身份独立，所属引擎退出、端口29783释放。Full128及其它冻结部署未改。

## 身份

- 私有attempt：`native-combat-1792735946432506000`。
- launch SHA256：`25c042a2ba600c693b523d7415de4a4196fa5c042886f49fef1590ec8bd49a87`。
- download SHA256：`dd135d0c1e4cda9e0eeaac52b9a393aff541ec8cc3b6f61a258f571de448da63`。
- engine SHA256：`fbd97f8f02621d37adc41b9782440950d9e0346ad2cffcb68601c01fb237141e`。
- digest scope：`native-payload+global-mt2504.v1`，不是全进程内存摘要。

原始帧、全局hex、合法掩码和第三方引擎保持私有；公开实现与本摘要。
独立原生重验需要私有完整归档及上述外部SHA，公开fixture测试不代替原生证据。

## 工程检查

- 55项专项测试通过，10.64秒：合法掩码、取消、同tick覆盖、完整分支与归档拒收。
- 包含空WAIT轨迹拒收、双分支同时篡改、丢tick、预约/RNG/epoch/进程身份篡改，
  以及重新计算外层回执后仍拒收源码、引擎、分支、进度和清理结果的检查。
- 与种子/洗牌/跳转入口/全局随机诊断及快照测试联合240项通过，20.67秒、零跳过。
- `tests-initial.xml`保留最初28项fixture构造错误；`tests-final.xml`为修正后的28项。
  后续`tests-verification.xml`及`tests-combined.xml`包含新增整包验收检查，集合不累加。

## 结论边界

两条主轨迹全局MT均未观察到变化；这是只读观测，不证明固定速猪不使用该对象，
也不证明其它随机状态全部可恢复。固定卡组实际消费者、一般pending/fallback
信念路径、隐藏随机全覆盖及Full128完整原生终局仍待验。
`fixed_hog_usage_proven`、`hidden_random_coverage_closed`、`training_admissible`、
`gates_closed`继续为false；G0/G1与34/43限定登记不变，G2–G8未启动。
