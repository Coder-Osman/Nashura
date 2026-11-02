# 阶段成果与证据索引

## 2026-11-02：SSPI 静态机制编码缓存与 FAIR 摘要快路径（实机对照）

基于 main@4461fb05c956a17988f337ee2e862d154f9775b8，针对上一轮 128 模拟剖析的两项可复用热点实施优化。SemanticEncoder 仅在 eval 且 inference_mode 下缓存 EffectSemanticEncoder/MechanicProfileEncoder 的静态表示；缓存由模块/参数/buffer身份、版本、device、dtype和形状约束，模式切换、模型迁移、权重载入、autocast、训练及梯度路径均清除或跳过。FAIR SHA 对 [4,576,5] PLAY 掩码不再调用 tolist()，改为生成旧嵌套 bool 列表等价的语义摘要字节；不缓存外层摘要，每次都重读输入内容，避免外部可变 tensor 绕过版本计数。旧语义摘要 SHA 保持不变。

Apple M4 Max/macOS 15.6、Python 3.13、PyTorch CPU 单线程，同 seed 20261103、同模型权重；完整 V4 tensorizer 固定工程 fixture 的 FAIR SHA 为 5bc94882e9963f6afa032773d7bae9469c5e1efb81a99d13f5387aaf357a5fd8，ready hand 4，plays shape [4,576,5]。前向先预热，再交替顺序做7轮配对、每轮每版10次；旧/新摘要交替顺序7轮，每轮10次。

| 路径 | 旧实现 | 新实现 | 比值 |
| --- | ---: | ---: | ---: |
| 暖态模型前向 | 80.185 ms | 15.591 ms | 5.14× |
| FAIR SHA（每次重读输入） | 25.494 ms | 0.900 ms | 28.33× |

微基准为本地工程 fixture，不是实局。正确性定向测试 25 passed：策略/WDL logits、合法联合分布、采样动作/RNG、两轮策略CE/WDL CE的逐参数梯度、AdamW参数更新和checkpoint模型/优化器状态恢复相同；摘要严格等于旧语义，含外部张量写入的变更也会反映。主工程检查 273 passed；公开检查 1104 passed、42 skipped；来源清单2828项通过；gitleaks扫描约42.4 MB无发现。

随后复用了单独隔离的 Engine5 / port 26794 顺序真实根与 belief worker 探针；不需要其它 root/belief worker。基线和优化各跑两次、固定 seed 20261103、persistent session-v1、每次128次 FULL 有效模拟。基线墙钟57.780/57.397秒，优化24.611/24.739秒；两侧中位57.588/24.675秒，单根减少32.913秒或57.15%。模型SHA 9e1751...ac445e4、root FAIR SHA df7486...6457d6及完整32候选分布逐项一致，selected action均为 card 26000030 / cell232 / delay slot2；每根均2812实际belief ticks、2812次advance、0 discarded edges。FAIR snapshot round-trip均一致，belief lease均释放，native snapshot handle为0。各RPC命令次数逐项一致：step2813、observe-atomic2943、observe278、snapshot-create213、release-snapshot213、restore130、other148。最主要阶段中位数：inference 10.867→2.007秒；transition 33.578→18.272秒（其中对手策略采样/叶推理为嵌套诊断，不能再次相加）。这是单个随机开局状态的两次复测，尚非多状态平均或训练表现结论。

本阶段为 HOT_PATH_NATIVE_SEARCH_CONTROLLED_PASS；未做完整训练或策略强度评估。测量探针配置的是已核验空闲 Engine5 测试worker；没有停止/重启 AVD、adb或其他engine，也没有改变端口转发。详见原生对照证据 SSPI_SEARCH_HOT_PATH_NATIVE_CONTROL_2026-11-02.json、微基准报告 SSPI_SEARCH_HOT_PATH_OPTIMIZATION_2026-11-02.md及微基准证据 SSPI_SEARCH_HOT_PATH_OPTIMIZATION_2026-11-02.json。

## 2026-11-02：SSPI 搜索细分剖析

基于826a24b，在同一独立Engine5开局root、CPU单线程、seed20261103上串行完成delta/full各128次有效模拟，无额外native请求。原候选顺序、priors、Q、visits、所选动作、模型/FAIR SHA、2812tick、RPC计数全部相同，快照句柄最终0。剖析轮墙钟41.017/39.030秒，包含新增计时且负载未隔离；不把此前42.505/41.283秒的一次差异解释为稳定加速。

delta轮341次model.forward共22.027秒，其中MechanicProfileEncoder重复计算14606条静态operation/634个profile达17.727秒；348次顶层SSPIInformation.sha256共5.776秒，主要来自合法掩码转嵌套布尔列表后递归json.dumps。原子捕获full/delta为136139184/132435873 bytes，仅少2.72%；纯JSON解析0.2418/0.2290秒，完整原子校验0.9565/0.9725秒。开局短分支没有长局的大量累计历史，restore重置增量游标，RPC次数保持。详见[拆解](SSPI_SEARCH_BREAKDOWN_2026-11-02.md)和[机器摘要](SSPI_SEARCH_BREAKDOWN_2026-11-02.json)。这是诊断成果，未实施缓存、未更新权重、不计为完整训练或实力验证。

## 2026-11-02：SSPI 当前状态与新增事件（真实增量协议）

基于 e64d277，新增七环 JSON 增量控制命令及 resident batch 握手，宿主保存每槽/每消费者确认游标和有界 combat 因果摘要，不重建旧完整事件；保留当前对象、手牌、运行时事实和全部协议/FAIR检查。正常 collector/experiment 可显式使用 event-json-delta，belief 对应 persistent-delta；冻结运行配置保持不变。恢复失败保留游标、成功才重置，重连继续交付；修复未启用公开出牌消费者的陈旧游标参与合并检查的问题。初版新控制命令的大缓冲区逐次清零改为复用。

同一最终 probe、Engine5、固定CPU随机模型与随机流，按 full→delta 顺序各运行一次自然终局诊断：

| 指标 | 全量 JSON | 当前状态＋新增事件 |
| --- | ---: | ---: |
| collect_episode 墙钟 | 95.8739 s | 63.0756 s |
| 原生推进 | 4,711 ticks | 4,711 ticks |
| 批量捕获 | 434 | 434 |
| 响应字节 | 2,952,847,074 | 71,792,241 |
| JSON解析＋完整校验 | 23.2828 s | 0.7989 s |
| 接收（含 request ack） | 11.1095 s | 0.9763 s |
| 捕获通道总计 | 34.4029 s | 1.7847 s |

本对照响应减少97.57%，解析校验减少96.57%，整局墙钟减少34.21%。初版 probe 的反序 delta→full 对照为60.25/94.48秒；两版probe不可混合计算单一均值。后台系统负载未隔离，速度仅代表本机这些已记录场景，不外推所有局面。四次均564条动作/随机种子/逐决策FAIR SHA相同，动作SHA为9761409f96376ea8a0aa2eda394af29b4d08fb1fa497b1e9bede8ab6f03f486f。终局tick4801、未截断，但相同原生出牌拒绝使全部隔离，0个有效新训练/评估episode。

另行进行同轨迹、同捕获次数且无额外RPC的独立边界审计：tick90至4801共435个捕获边界的当前状态、七环新增事件、完整规范化combat记录及564条动作逐项一致。仅归一化跨运行的generation/epoch、各环序号基点及进程级deploymentSequence基点。首次遗漏deploymentSequence基点导致133处combat摘要不同，失败证据保留；修订后独立重跑通过。审计额外摘要开销不混入上表计时。

最终probe另完成全量/增量128有效模拟的受控搜索。完整候选顺序、先验、Q、访问分布、所选动作和根FAIR SHA逐项一致；实际推进均2,812ticks，212次对手策略采样、129次叶节点/根推理、130次restore、213次snapshot-create/release，结束句柄为0。

| 搜索顶层阶段（秒） | 全量 | 增量 |
| --- | ---: | ---: |
| 整根墙钟 | 42.5055 | 41.2835 |
| belief | 1.3266 | 1.2700 |
| candidate_generation | 0.4901 | 0.4592 |
| tree_selection | 0.0066 | 0.0064 |
| inference | 8.4219 | 8.2604 |
| restore | 1.0361 | 0.9943 |
| transition | 25.1996 | 24.2986 |
| 顶层未归类 | 5.9985 | 5.9679 |

嵌套RPC：step均2,813次（含初始化90tick），2.3246/2.1888秒；observe-atomic(-delta)均2,943次，2.7375/2.5798秒；restore均130次，0.1613/0.1435秒。transition扣除不重叠的opponent_sample和rollout.advance后，剩余3.1298/2.9398秒；advance内吞吐344.45/360.87tick/s，按整根墙钟计66.16/68.11tick/s。嵌套数据不再加到顶层总时间。开局短分支历史少，本次只观察到2.9%的根墙钟差异，不以长局传输改善比例冒充搜索加速。

搜索验收使用单个独立Engine5进程：先冻结实际开局FAIR证据，再独立构建belief，无实际隐藏状态复制；快照恢复FAIR/手牌合法状态和句柄释放通过。未做双进程实际根物理状态保留验收。所有对照在新模型/新局上运行，不引入第二训练组。

工程验证：受影响定向326 passed；主工程248 passed；公开九套件1104 passed、42 skipped；gitleaks扫描42.02MB无发现。原生编译成功，初版probe SHA d40e4f8ab085900d49689f31cbeab88bbd12a120fc1b9818fda05411f89eb4b6，最终be2eff5ef7d9a9656ddc98b6340e0307d541a0084d5d9ded5746fee743313674；libg SHA保持110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783。二进制不发布。

部署事故：第二次重载时误用包级am set-debug-app，Android终止了原六个引擎进程。执行前无collector/evaluator，已终局槽与空闲引擎的内存状态仍因此丢失；落盘证据/权重、AVD、adb、原转发及全局APK文件保留。已清调试设置并重启已有组件，冷启动状态另记录，不把重启冒充原状态恢复。所有事故及失败证据保留，详见[协议与限制](SSPI_JSON_EVENT_DELTA_2026-11-02.md)。

后续尚需：同版本actual及两belief完整预检、有效混合采集终局、仅搜索根更新/checkpoint恢复和新版无搜索评估；现有原生拒绝及旧观测getter更新遥测缓存的问题需分别处理。本轮是传输协议与有限搜索工程验收，不是训练闭环或模型强度完成声明。机器身份与计时详见[证据摘要](SSPI_JSON_DELTA_EVIDENCE_2026-11-02.json)。

## 2026-11-02：SSPI 旧事件重复校验与最小历史审查

基于 `1d7b021`，在独立发布工作树实现三项宿主优化：`FinalBattleEnv` 的双方观测直接使用 owner 隔离事件窗口并复用同 tick 元组；Tracker 先按时间/可见性过滤，再计算身份，去重集合只保留当前 tick；原子捕获按 epoch、序号和完整记录字节缓存 phase 字段校验。使用 `pickle.dumps` 作类型精确比较，不执行反序列化。新记录或修改记录仍完整校验，环头、序号连续性、错误、时间、owner 隔离及快照恢复条件保留。此为有界宿主缓存，线上捕获格式及字节量未变。

Mac ARM64 AVD 上以固定随机模型、seed `20261103`、相同 run/game/random-stream、CPU 单线程、0% MCTS、resident 槽 2 和 `event-json` 完成三组到自然终局的对照。六次均为 4711 native ticks、434 次捕获、564 条动作，动作/随机种子/逐决策 FAIR SHA 全量一致，终局 tick 4801；每次响应 2,955,400,491 bytes。

| 轮次 | 基线墙钟 | 优化墙钟 | 基线解析+校验 | 优化解析+校验 | 限制 |
| --- | ---: | ---: | ---: | ---: | --- |
| 1 | 96.84 s | 93.81 s | 26.43 s | 22.66 s | 基线在释放旧槽前，优化在释放后 |
| 2 | 155.02 s | 143.21 s | 58.12 s | 43.46 s | 双方均带终局 `sys.settrace` 诊断 |
| 3 | 142.04 s | 102.34 s | 39.77 s | 25.11 s | 双方已释放旧槽、无追踪；优化先跑 |

第 3 轮实测墙钟减少 27.95%，解析+校验减少 36.85%，但接收时间也从 17.01 变为 12.50 秒，后台系统负载没有隔离。三个轮次存在显著波动，不混合求平均，也不将第 3 轮比例视作稳定、全部由代码产生的加速。第 3 轮捕获总时间为 56.79/37.62 秒，包含解析+校验及接收；`request_ack_ns` 又嵌套在接收计时中，禁止重复相加。

为分离网络/游戏成本，在 tick 4801 额外只读捕获一次，确认原生槽状态不变；对同一 9,669,028-byte 输入先各预热一次，再交替顺序运行 20 对 `orjson.loads` + 完整原子校验。中位数基线 124.30 ms、优化 92.91 ms，减少 25.25%，返回结果完全相同；优化首次冷校验为 142.53 ms，热缓存含 4096 条 phase 记录。这只证明同输入重复解析/校验的局部收益，不包含 RPC、游戏推进或首次新事件成本。

六次均自然 ended、未 truncated，但现有洁净终局准入将其隔离；两次带追踪运行均记录相同四次原生出牌拒绝（tick 178、2182、3018、4402）。因此本轮 **0 个有效训练/评估 episode**，没有更新权重或模型强度结论。用户授权释放的旧槽 0/1 已关闭，释放前后槽 2 保持 tick 4801；AVD、adb、probe 及其他 native worker 保留。原始隔离记录、动作和捕获仅保存在本机，公开[机器摘要](SSPI_HOST_HISTORY_EVIDENCE_2026-11-02.json)记录计时、动作 SHA、模型/原生/源码身份及局限。

最终主工程检查 `226 passed`，公开九套件合计 `1,104 passed、42 skipped`。扩展定向首次 `309 passed、1 failed`：`test_fair_final_phase_snapshot.py` 缺少被忽略的本机 `phase_runtime_telemetry.inc` 附件；复用现有附件后该项单独 `1 passed`，附件 SHA 和初始失败证据已记录，附件不发布。最初误用系统 Python 3.9 的收集失败也保留；最终全部检查使用已有 Python 3.13.5 环境，无依赖变更。

本轮公开源码清单 2,814 项校验通过；`gitleaks dir` 扫描 41.87 MB 无发现，`git diff --check` 通过。

历史不应反复发送整份遥测环。Tracker 需要圣水区间、轮转/可用性及当前游标；当前战场还需要王塔激活、攻击阶段与活跃来源关系等摘要。下一层应传当前状态与四类事件环增量，独立消费者确认后丢弃旧载荷，快照同步保存摘要和游标。当前公开 compact producer 的 phase 窗口为空，visibility/remaining 尚未齐备，尚未证明与 FAIR 语义等价，故本轮未切换协议。详见[最小必要历史方案](SSPI_MINIMAL_HISTORY_2026-11-02.md)。

## 2026-11-02：SSPI 观测与评估开销审查及局部优化

基于 `main@aa45dbf` 审查 SSPI 采集/搜索/关闭搜索评估路径。`wake_refills=False`，补牌本身不产生额外决策；可信补牌时间边界仍需真实捕获校验，但不调用策略。训练的 FAIR 公共历史继续在每次原生捕获边界更新。双方同时冻结时，采集器现在直接复用 runtime 的双方 FAIR 视图；仅一方待决策时只补另一方视图；非决策捕获保持一次双方视图。终局复用最后一次完整观测。`FinalRollout` 提交已冻结动作时不再重复复制整帧。

关闭 MCTS 的评估不再创建或更新搜索历史，也不再预检无用的 belief 端口；actual 端口仍在 reset 前完成 status、只读 observe 和 libg/probe/ruleset 身份绑定。评估继续在真实决策边界生成 FAIR 模型输入，并在终局核对双方 WDL。相关 SSPI、FAIR runtime/rollout/history、时序增密回归 `108 passed`；公开检查合计 `1,104 passed、42 skipped`，主引擎 `214 passed`，源码清单 `2,810` 项，gitleaks 扫描 41.22 MB 无发现，`git diff --check` 通过。这是调用路径与局部测试结论，未实测新版完整局的墙钟收益或模型强度。

既有两局事件批量无搜索评估分别记录 3511/6002 native ticks、326/704 次完整捕获，响应体 1.98/5.51 GB，JSON 解析与完整捕获校验 23.97/54.70 秒；这些是优化前的诊断数据，不是本次收益。训练中已执行 PLAY 的 200 ms 中间样本仍保留必要的独立张量化。另有新版采集在 tick 118 因真实 resident 轨迹与独立 legacy 信念世界 FAIR 状态不一致而隔离；事件批量推进及捕获替换须先解决该等价问题，本轮不把截断或隔离局当作训练结果。

后续在 Mac ARM64 AVD 同一独立 resident 槽 2，以 `aa45dbf` 与 `f4cdba3`、同一随机初始化 seed `20261103`、同一随机流和 0% MCTS 做两轮反序实测。两版各次均执行 3511 native ticks、302 次完整捕获，409 条动作日志及 FAIR SHA 逐条一致，终局 tick 3601。优化前耗时 76.29/76.56 秒，优化后 66.78/67.07 秒；均值 76.42 降到 66.93 秒，节省 9.49 秒或约 12.4%。第二轮优化版接收并解码 1.90 GB 完整 JSON，捕获总耗时 24.02 秒，其中 `decode_ns` 16.97 秒；基线捕获总耗时 24.33 秒，其中 `decode_ns` 16.93 秒。这里的 `decode_ns` 包含 `orjson.loads` 和完整原子捕获校验，不能当作纯 JSON 解析时间；本轮优化没有减少原生响应体大小。两版在 tick 3118 均有同一原生出牌拒绝，终局按洁净边界规则隔离，因此上述数值只证明相同轨迹到终局的局部运行成本，不是有效完整 episode 的吞吐或模型强度。槽 0/1 的原状态 tick 114/118 保持未变，失败产物留在本机独立临时目录。v5 训练使用 `persistent` 传输，一局 26 个搜索根、耗时 3577 秒；搜索根合计 3056 秒，`transition` 合计 2054 秒（整局 57.4%），叶推理合计 179 秒（整局 5.0%）。`transition` 包含推进、RPC、观测和解码等，训练未记录可比的独立 `decode_ns`；不能据此断言训练没有观测开销或给出准确解码占比。

## 2026-11-02：SSPI 补牌时序协议修订（工程验证）

在最新 `origin/main` 派生工作树实现无补牌决策的 Hog 时序输入：稳定八卡身份、四个物理槽、待补 FIFO、剩余 tick/已知标志；无 EMPTY 动作。模型可提议待补牌的五档 PLAY；若预测到位 tick 晚于或等于提议出牌 tick，则忽略提议并按 WAIT 推进，无额外负反馈。搜索世界与实际对局使用同一适配器；采集日志区分提议与执行动作。新版输入 schema 改变，旧 v5 的四局局部 checkpoint 与新模型架构身份不兼容，不能据此声称已对新版完成训练。

Mac M4 Max + ARM64 Android Emulator `astral-api30` 的事件批量 resident 端点 `26789` 补牌探针通过：预测与原生均在 tick 114 到位、`woke_owner0=false`、一次过早 PLAY 被忽略且未提交原生预约。独立 legacy 端点 `26790/26792` 上的新版真实开局搜索根 128/128 有效，45.92 秒、2812 实际 native ticks，0 丢弃边；原生根摘要前后一致、信念句柄释放。搜索内阶段为 transition 29.21 秒、叶推理 7.62 秒、restore 1.33 秒、信念构造 1.26 秒、候选生成 0.46 秒；RPC 与对手采样计时嵌在这些阶段内，不重复相加。模型 7,869,902 参数，libg SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`，固定 seed `20261103`，工程根 `training_admissible=false`。相关 SSPI、FAIR 搜索/快照与时序增强扩展定向回归 `171 passed`。这验证协议及真实搜索根；新协议的自然完整局、参数更新和关闭 MCTS 的独立评估尚未完成。

同一新版随机模型的 CPU/MPS 小型前后向及优化器步均通过，CPU/MPS 三次更新中位分别为 0.122/0.151 秒；WAIT 对数概率差 0，WDL 最大绝对差 `2.98e-8`，当前选 CPU。此为合成 WAIT/WDL 算子探针，不是训练样本。本次公开检查 `1104 passed, 42 skipped`，主引擎 `214 passed`，源码清单 `2810` 项，gitleaks 扫描约 41.20 MB 无发现，差异空白检查通过。

## 2026-11-02：SSPI 终局自动流水线、worker 预检与 owner provider 复用（真实局进行中）

`experiment` orchestrator 等待完整自然终局后，自动执行冻结策略训练采集、验证、仅搜索根的 policy CE/WDL CE 更新、checkpoint 保存与恢复及关闭 MCTS 的 32 对/64 局严格配对评测。完整评测后 heartbeat 按同一协议续接后代；三代只是首次观察里程碑，没有固定墙钟上限。评测恢复入口验证 generation 记录、checkpoint 文件 SHA、optimizer 状态及 theta0 初始化。截断局只隔离，不作为平局或训练标签。

采集器为 owner 0/1 分别保留独立 `SSPIHistorySearchProvider` 和 native belief 端口；owner 切换后复用对应 FAIR 历史游标，结束或异常路径释放全部 provider。实际端口映射进入 runtime SHA 和 episode identity。新增 reset 前的三端预检：验证 ready、只读观测可完整解析、ruleset manifest 与 native libg/probe identity 一致。SSPI Mac training/时序增强回归 `58 passed`。

v3 使用 7,874,126 参数 CPU 模型及 seed `20261103`，但在 owner 1 belief 端口 26791 的 `observe` 返回截断 JSON 后隔离；该端口复测 status 成功但 observe 稳定失败。v4 因独立进程缺少 `CR_PROBE` 环境变量，在 native reset 前因 ruleset manifest 未绑定 probe 而失败。两者均无完整 episode、训练更新或可用 checkpoint，未纳入训练。修复后的预检在 26789/26790/26792 上通过完整 ruleset attestation，libg SHA `110aa2…959783`、probe SHA `f5a691…f346f` 一致。

v5 为全新 run ID/目录，使用未训练随机初始化 seed `20261103`，明确固定本机已验收 probe 路径和健康端口 26790/26792。当前 collector PID 37526 和 orchestrator PID 37559 均存活；最新日志推进至原生 tick=1243，完成 8 个 SEARCH 根。自然终局尚未产生，所以完整训练样本、参数更新、checkpoint 恢复、配对评测和模型强度仍未验证。CPU/MPS 小型更新对照此前均通过，CPU `0.105s/update`、MPS `0.215s/update`，选择 CPU。

本次完整发布检查通过：SSPI Mac training/时序增强 `58 passed`；公开测试合计 `1,104 passed, 42 skipped`（历史 FAIR 主组 794/27、affordability 23、selection-timeout 82、position guards 15/3、live command 25/4、dispatch 31/3、reservation mapping 31/2、command coordinates 38/3、collection coordinator 65）；主引擎 `212 passed`；源码清单 `2,810` 项；gitleaks 扫描 38.95 MB 未发现泄露；`git diff --check` 通过。

## 2026-11-02：SSPI 已执行动作的中间观测增密（工程验证）

实现 200 ms 中间观测采样：仅保留最终执行的 SEARCH PLAY 计划，使用剩余延迟动作与实际终局 WDL 训练。用户确认每组原根权重 0.5、所有派生点均分另外 0.5；没有派生点时原根权重 1，曝光额度仍按源根计算。新增采集器、序列化及学习器对接，旧 episode 兼容；运行中的冻结部署保持原样。普通策略时钟模拟、真实 FAIR 张量化及完整模型反向更新等定向检查 66 passed；未运行新原生整局或胜率评估，未宣称实战提升。详见[实现与测试记录](SSPI_TEMPORAL_AUGMENTATION_2026-11-02.md)。

## 2026-11-01：SSPI搜索性能优化（限定工程子项，实机验证通过）

新增条件概率缓存、向量化稳定候选排序、候选/树选择时间、真实tick/RPC及对手推理计时，并在M4 Max Android AVD完成同FAIR根1024对照。base legacy 486.01秒、PR legacy 470.88秒、PR persistent 403.81秒；三组完整访问分布和selected action一致、均1024/1024有效。session-v1跨snapshot/restore/observe-atomic状态等价门通过，默认仍为legacy。性能与嵌套时间证据见[实测记录](SSPI_SEARCH_PERFORMANCE_HANDOFF_2026-11-01.md)。工程子项`SEARCH_PERFORMANCE_CONTROLLED_AVD_PASS`；P2完整混合对局、P3参数更新/checkpoint恢复和P4关闭MCTS评测仍未运行，不构成整体训练验收或模型强度结论。


## 2026-11-01：SSPI 动作协议与真实 1024 搜索根（限定工程子项）

在 Apple M4 Max 的 macOS ARM64 Android Emulator 上完成 SSPI Hog 动作/手牌协议定向回归和首个真实 PUCT 搜索根：7,874,126 参数，1024/1024 有效模拟，712.48 秒，0 次未完成边丢弃；根 FAIR 状态恢复一致，独立信念快照已释放。成本分项为转移495.44秒、推理94.34秒、快照恢复16.62秒、信念构造1.92秒。CPU/MPS 小型梯度步中位数为0.174/0.217秒，选用CPU。定向测试73 passed，编译与差异检查通过。该根为 `training_admissible=false` 的独立工程验收；完整混合对局、终局训练更新、checkpoint恢复和关闭MCTS评估未运行，不构成整体门或模型强度结论。详见[阶段报告](SSPI_MAC_SEARCH_P0_P1_2026-11-01.md)。

## 2026-11-01：SSPI Mac Pilot 设计状态（非实战结果）

已完成文档修订：本轮仅固定2.6速猪、无英雄技能；3模式动作协议保留ABILITY扩展入口且当前始终掩码；2/3张手牌与延迟补牌仍是正确性门槛。详见 [训练规格 v1.1](SSPI_MAC_PILOT_TRAINING_SPEC_2026-11-01.md) 与 [术语](../CONTEXT.md)。

环境已修订为macOS本机 ARM64 Android Emulator（AVD），参照 [既有Mac实测记录](../firstlight_CR/docs/local_mac_capture_training.md)，不使用VMware Ubuntu，也不预设MPS优于CPU。**当前这次修订没有实机检查现有AVD是否仍可用**。

训练规格 v1.3 取消所有硬性墙钟时长限制：三代/16+4局为初始观察里程碑，之后按真实学习曲线及机器资源状态继续，允许在完整边界停止并保存证据；不把时间额度当作训练完成判据。

当时状态：`DESIGN_REVISED_NO_TIME_CAP / MAC_AVD_NOT_RECHECKED / IMPLEMENTATION_NOT_RUN / NATIVE_1024_NOT_RUN / LEARNING_NOT_RUN`。这些标记描述该文档提交时的状态；后续实测与未完成项见本节前的当前阶段记录。


## 2026-10-29：Android ART 闭包启动与 Binder 阻塞

同构 Ubuntu ARM64 guest 中，ART 闭包完成 `/proc` bind mount、`pivot_root` 及 linker
依赖解析；`app_process64` 已装载 `libandroid_runtime.so`、`libart.so`，发现 boot image
相关文件并选择 default boot image 配置（打印 `Using default boot image`）；调用栈已进入 `JNI_CreateJavaVM`，随后
在 guest 的 `/dev/binder` 接口处停止。默认路径为 `ENOENT`，占位普通文件对照的
`BINDER_VERSION` ioctl 为 `ENOTTY`，进程退出码为134（`SIGABRT`）。因此阶段状态为
`BLOCKED_GUEST_BINDER`，JavaVM 指针未返回，JNI 生命周期为 `NOT_RUN`，Linux holder setup 继续
为 `RUNTIME_BLOCKED_JAVAVM`，`CATALOG_READY_PASS=false`，ResourceManager、GameState、
Step、完整对局和性能保持 `NOT_RUN`。

详见[阶段报告](../agent-handoff/ANDROID_ART_CLOSURE_APP_PROCESS_BINDER_BLOCK_2026-10-29.md)
和[机器证据摘要](../agent-handoff/android-art-closure-app-process-binder-evidence-20261029.json)。

## 2026-10-29 E2.1-N：Linux holder setup 真实执行与 JavaVM 阻塞

恢复 guest 登录通道后，真实 ARM64 Linux probe 完成内容 root、filesystem 和
data/update provider 注册；holder setup 在 allocator 和首个对象分配后于
`libg.so+0x17d8b14` 读取空 `JavaVM` 指针并收到 `SIGSEGV`。输入、资源、辅助文件和
probe 前后 SHA 均未改变，状态为 `RUNTIME_BLOCKED_JAVAVM`。这不是 catalog ready、
ResourceManager、GameState 或完整对局证据；这些门保持 `NOT_RUN`。

详见[报告](../agent-handoff/PURE_LINUX_HOLDER_SETUP_JAVAVM_BLOCK_2026-10-29.md)和
[机器证据](../agent-handoff/pure-linux-holder-setup-javavm-block-evidence-20261029.json)。

## 2026-10-29：worker2 后续工程复验

Python 3.13.5 下重新运行公开工程门：纯 Linux 专项 `1105 passed、1 skipped`，
公开主组 `794 passed、27 skipped`，主引擎专项 `211 passed`，affordability、
selection timeout、position guards、live、dispatch、reservation、coordinates、
collection coordinator 分组均 exit 0；公开快照和 `gitleaks` 通过。guest SSH 仍拒绝，
所以 Linux holder setup 仍为 `BLOCKED_ENVIRONMENT / NOT_RUN`，没有生成新的 Linux
运行日志；严格 `CATALOG_READY_PASS=false`，其余原生对象图保持 `NOT_RUN`。详见
[复验记录](../agent-handoff/CODEX_REPLY_2026-10-29_REVALIDATION.md)。

## 2026-10-29 E2.1-N：Linux 原生持有者设置阶段

Android holder setup 追踪稳定通过：事件无丢失，设置槽从空变为非空并与同线程
后续初始化关联，update 清单前后均为908项。holder setup 的 Linux probe、版本
guard、输出契约和审计器已加入；但 VMware Ubuntu ARM64 guest 的 `vmrun` 操作缺少
有效 guest 密码，SSH 22 端口拒绝，本轮没有生成 Linux 结果或日志，因此阶段状态为
`BLOCKED_ENVIRONMENT`，不声称 Linux setup 已执行。纯 Linux 专项为 `1105 passed、
1 skipped`，严格 `CATALOG_READY_PASS=false`，其余完整引擎门保持 `NOT_RUN`。
详见[报告](../agent-handoff/PURE_LINUX_HOLDER_SETUP_2026-10-29.md)和
[机器证据](../agent-handoff/pure-linux-holder-setup-evidence-20261029.json)。

## 2026-10-29 E2.1-N：Linux 原生持有者工厂生命周期

真实 ARM64 Linux 子进程完成参数工厂、持有者工厂的创建与清理，并通过 Android
参数来源和 Linux 输出的 SHA/顺序审计。阶段状态为 `NATIVE_VERIFIED_LIMITED`；严格
`CATALOG_READY_PASS=false`，配置异常、Backup 等价、ResourceManager、GameState、
Step、完整对局及性能验收保持 `UNRESOLVED/NOT_RUN`。相关测试为1094 passed、1 skipped。
详见[报告](../agent-handoff/PURE_LINUX_NATIVE_HOLDER_FACTORY_2026-10-29.md)和
[机器证据](../agent-handoff/pure-linux-native-holder-factory-evidence-20261029.json)。

## 2026-10-29 E2.1-N：完成函数查询键

双端30字节键一致，查询结果仍不同。详见
[报告](../agent-handoff/PURE_LINUX_FINISH_LOOKUP_KEY_2026-10-29.md)。

## 2026-10-29 E2.1-N：完成函数查询返回

同一候选查询 Android 返回非空、Linux 返回空；原因和业务语义待查。详见
[报告](../agent-handoff/PURE_LINUX_FINISH_LOOKUP_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 完成调用

17槽的1,417行差异出现在 `finish_a` 调用区间。详见
[报告](../agent-handoff/PURE_LINUX_CATALOG_FINISH_PHASES_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 差异批次边界

前4批与第5批入口一致，第5批返回出现1,417行差异。详见
[报告](../agent-handoff/PURE_LINUX_CATALOG_BATCH_BOUNDARIES_2026-10-29.md)。

## 2026-10-29 E2.1-N：匹配 update 的 catalog 布局

Android 与 Linux 清单均为相同 908 项时仍有 1,457 行差异。详见
[报告](../agent-handoff/PURE_LINUX_MATCHED_UPDATE_LAYOUT_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 批次返回序列

Android/Linux 5 批次的 313 次 getter 返回序列一致；旧观察器回调异常已修复并
记录。详见[报告](../agent-handoff/PURE_LINUX_CATALOG_ENTRY_COMPARISON_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 批次内入口

Linux 完成限定 getter 观测；Android 附加超时，跨平台比较未知。详见
[报告](../agent-handoff/PURE_LINUX_CATALOG_SCOPED_ENTRY_TRACE_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 资源对象类型

独立 Android/Linux 观察的597条逻辑路径类别对应；不是字节或解析等价。
详见[报告](../agent-handoff/PURE_LINUX_CATALOG_RESOURCE_SELECTION_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 启动参数

两侧真实进程5组批量参数及597项逻辑资源请求路径集合一致；内容和输入差异待查。
详见[报告](../agent-handoff/PURE_LINUX_CATALOG_BATCH_TRACE_2026-10-29.md)。

## 2026-10-29 E2.1-N：catalog 行对象字段差异

17槽 Linux 的2,078项字段均包含于 Android 对应槽；Android 多1,457项。输入门和
字段完整语义仍未验收。详见[报告](../agent-handoff/PURE_LINUX_CATALOG_ROW_FIELD_2026-10-29.md)。

## 2026-10-28 E2.1-N：Android update 清单变化

两次启动复现990项变908项，表布局重复；两次均未通过身份门。详见
[报告](../agent-handoff/PURE_LINUX_CATALOG_UPDATE_DRIFT_2026-10-28.md)。

## 2026-10-28 E2.1-N：catalog 实际布局差异

Android 和 Linux 各162槽，17槽行数不同，合计分别25,810和24,353；根因未确定。
详见[报告](../agent-handoff/PURE_LINUX_CATALOG_LAYOUT_DIFFERENCE_2026-10-28.md)。

## 2026-10-28 E2.1-N：update 资源身份对照

历史 Android 与 Linux 的990项 update 文件逐项 SHA 一致；Linux loader 打开的131项
均匹配。详见[报告](../agent-handoff/PURE_LINUX_CATALOG_UPDATE_IDENTITY_2026-10-28.md)。

## 2026-10-28 E2.1-N：APK 资源身份对照

历史 E1 与用户 APK 的9,829项资源逐项 SHA 一致；Linux loader 的619项暂存清单
也与 APK 一致。update 和 catalog 表值未对照；详见[报告](../agent-handoff/PURE_LINUX_CATALOG_APK_IDENTITY_2026-10-28.md)。

## 2026-10-28 E2.1-N：原生队列单边界中断恢复

ARM64 Linux/Bionic 实测发布后进程中断、显式恢复及原生归档通过；仅覆盖同 inode
双链接状态，不代表 Android Backup 或完整 catalog 等价。专项988通过/1跳过。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_CRASH_RECOVERY_2026-10-28.md)。

## 2026-10-28 E2.1-N：Android 与 Linux 资源局部对照

24个独立真实引擎资源案例与历史 Android E1 归档的字节和文件操作匹配，独立
校验通过；不代表 FMOD、完整 catalog 或 Backup 等价。专项981通过/1跳过。
详见[报告](../agent-handoff/PURE_LINUX_E1_PAIRED_RESOURCE_2026-10-28.md)。

## 2026-10-28 E2.1-N：真实 libg 合成资源路由矩阵

ARM64 Linux 原生引擎对13例合成 APK/update 冲突、缺失和回退按既有 E1 对应规则
运行；修复 probe 的无效文件 length 调用后13/13通过，独立验收exit0。
第三轮缺失例完成provider释放、dlclose及asset finish；Android配对字节及严格catalog仍未验证；Linux专项973通过/1跳过。
详见[报告](../agent-handoff/PURE_LINUX_RESOURCE_MATRIX_2026-10-28.md)。

## 2026-10-28 E2.1-N：Linux catalog 的资源路径观察

真实 libg 轨迹复算发现247次 APK open/read/close及131个 update 文件完整映射关闭；
122个 update 打开路径同时存在 APK 暂存副本，其中118个清单 SHA 不同。
未证明逐请求回退、映射字节消费或 Android 资源一致；严格catalog门保持关闭。
Linux专项964通过/1跳过。详见[报告](../agent-handoff/PURE_LINUX_CATALOG_RESOURCE_TRACE_2026-10-28.md)。

## 2026-10-28 E2.1-N：已有编码键的真实 libg 配置写入

修复非空偏好 XML 的键计数：使用绑定材料逐槽 AES 编码，不以明文键估算。
已有 Android/Bionic 对照 XML 与真实 ARM64 Linux libg 重跑，双槽实际与预期均1项，
两个commit、一次候选入队，native exit0；严格wrapper exit1。自然loader ready，
162张表/24,353行仅为布局观察，Backup等价和catalog严格门仍未通过。
专项957通过/1跳过；详见[报告](../agent-handoff/PURE_LINUX_NONEMPTY_PREFERENCE_RECHECK_2026-10-28.md)
及[机器证据](../agent-handoff/pure-linux-nonempty-preference-recheck-evidence-20261028.json)。

## 2026-10-28 E2.1-N：受控配置 store/restore 运行时复核

隔离 Android 30 ARM64 AVD 中观察到配置值从 `server-login` 写入测试值并恢复；
后续只读复核仍为原值。两轮各有两个槽写入，四次 commit 均true，且观察到
`commitToMemory → enqueueDiskWrite → writeToFile → createFileOutputStream`。
两次 Backup 请求进入服务端 `dataChangedImpl` 和 `KeyValueBackupJob.schedule`。
目标包此前已在30项pending队列中，计数未变，journal写入未观察；不证明本次journal
分支、交付、跨进程恢复或文件字节恢复。专项953通过/1跳过、主工程211通过、
公开回归1104通过/42跳过、清单2583项及gitleaks均通过，均exit0。
严格 `CATALOG_READY_PASS` 仍false，ResourceManager/GameState/真实Step均NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_CONTROLLED_STORE_RECHECK_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-controlled-store-recheck-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_CONTROLLED_STORE.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：当前身份与单事务逐字节一致

只读捕获两次一致，当前材料绑定到原Android事务与三个真实libg子进程。
真实store/恢复后文件与进程身份不变；新的两个Bionic重放进程各exit0，
目标双槽208bytes、恢复65bytes，共8项逐字节对照通过。
修复前4项恢复XML格式差异保留；修复后probe与旧libg批次probe分别记录。
专项951通过/1跳过、主工程211通过、公开1104通过/42跳过，均exit0；253份原XML不变。
当前材料身份取得限定证据，Backup服务与异常域仍未闭合；严格catalog仍BLOCKED。
ResourceManager、GameState、Step、完整局及10×均NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_RUNTIME_PREFERENCE_BINDING_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-runtime-preference-binding-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_RUNTIME_PREFERENCE.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：原生候选入队与自然 loader 重复观察

默认Backup未解析对照native96；两个显式候选native0，严格wrapper均1。
各自然loader53步、ready 0→1、162张表/24,353行；正常析构、asset finish及dlclose。
独立消费者各exit0、两槽各1086bytes逐字节归档后清pending；写入仅在独占副本。
新增43项工具测试，隔离专项900/1、主工程211、公开1104/42均exit0。
首次编译及权限拒收失败保留；候选归档不等价Android BackupManager。
完整配置契约与当前材料身份仍BLOCKED，严格CATALOG_READY_PASS未取得，
ResourceManager、GameState、Step、完整局和10×均NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_NATIVE_BACKUP_BINDING_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-native-backup-binding-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_BINDING.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：真实 agent 失败、重排与重试

一次自有store/request，真实IOException后目标pending保持且进入新journal；
第二次bmgr run成功消费，无追加请求，目标pending/journal消失。XML215bytes不变，
成功流259bytes、transport231bytes、state72bytes；头与删除方法次数UNKNOWN。
73份命令归档通过严格离线审计；v1采集缺陷、首次负例失败保留。
新增47项测试，隔离专项857通过/1跳过、主工程211通过、公开1104通过/42跳过，均exit0。
专用5582已关闭；Linux Backup binding及CATALOG_READY_PASS仍BLOCKED，后续对象图NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_RETRY_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-backup-retry-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_RETRY.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：非空 Backup 与队列消费

自有fixture实际非增量备份261bytes日志流、233bytes落盘；目标仍pending。
仅一次后续bmgr run消费队列，目标消失，原journal不再列出，四份存活journal目标0。
XML217bytes、state72bytes和transport在消费前后逐字节一致；header与删除方法计数未知。
新增37项工程测试；隔离专项810/1、主工程211、公开1104/42，三组exit0，252份原XML未变。
专用5582已关闭；Linux绑定与严格catalog仍BLOCKED，后续对象图未运行。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_PAYLOAD_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-backup-payload-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_PAYLOAD.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：JobService 回调实际交付边界

真实JobScheduler受控提前执行已排队job，捕获onStartJob→backupNow→cancel六条实验事件；
包含准备共10事件，零drop/error/active，原回调返回false不等于异步工作完成。
工作线程日志显示agent调用与pass结束，但0bytes不证明非空transport上传；
worker插桩、journal删除、失败路径与Linux服务绑定仍未闭合。
首轮system_server崩溃及未知原因保留；专用5582已关闭，旧实例和VM保留。
新增48项工具测试，隔离专项773通过/1跳过、主工程211通过、公开1104通过/42跳过，
三组均exit0，251份历史XML逐SHA未变。严格catalog仍BLOCKED，后续对象图NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_DELIVERY_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-backup-delivery-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_DELIVERY.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：Backup 服务正常分支真实观察

自有应用两个真实Binder请求覆盖首次pending入队/journal追加与重复去重；
同线程scope区分独立平台job提交，返回1不代表交付，跨线程强因果仍UNKNOWN。
八hook/24事件、零drop/error/active；journal95bytes、三条长度边界、目标一条。
新增36项工具测试；专项725通过/1跳过、主工程211通过、公开1104通过/42跳过。
六轮失败/覆盖不足记录保留，旧解释器失败单列，测试源码与提交源码一致。
专用AVD已关闭。Linux Backup绑定及CATALOG_READY_PASS仍BLOCKED；后续对象图NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_SERVICE_RUNTIME_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-backup-service-runtime-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_SERVICE.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：真实 journal 追加与重开局部一致

真实Android DataChangedJournal与ARM64 Linux/Bionic helper逐步比较：8条84bytes、
跨进程重开9条94bytes完全一致；两环境各两个进程exit0。写前/写后fsync有真实trace，
无断电持久化结论。异常用例输入差别、真实平台范围及合成故障注入边界单列。
专项689通过/1跳过，主工程211通过；公开回归与发布检查结果见本轮机器证据。
未重复游戏store或主动Backup，未触及服务pending，不重新加载libg。
Linux服务绑定及CATALOG_READY_PASS仍BLOCKED，GameState/真实Step/完整局NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_BACKUP_JOURNAL_RUNTIME_2026-10-28.md)、
[证据](../agent-handoff/pure-linux-backup-journal-runtime-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_JOURNAL.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：Backup 自然分支与 journal/scheduler 静态边界

45.006588917秒真实被动窗口：目标入口0、事件0、丢事件0、观察错误0；六hook已安装，
boot/starttime前后一致，结果NOT_OBSERVED。历史put返回保持未知，没有重复store或主动请求。
六个精确DEX方法的人工审阅绑定新增工具：journal IOException清空引用并正常返回，
schedule按user去重且忽略JobScheduler返回，因此正常返回不足以证明持久化或交付。
43项新增工具测试通过；完整回归和SHA见本轮机器证据。Linux实际Backup绑定及严格catalog
仍BLOCKED，后续对象图未运行。详见
[报告](../agent-handoff/PURE_LINUX_BACKUP_BRANCH_OBSERVATION_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-backup-branch-observation-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_BRANCH.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：Backup 队列去重语义

同隔离Android实例两轮只读检查各两次采样：目标已在pending、属于participants、journal对象存在。
未持锁采样不证明历史事务分支。真实services DEX的40指令方法经人工审阅并绑定SHA：
只有put原返回为null才调用journal；正常非null targets路径仍schedule，异常不保证。
25项新增边界测试通过；隔离全量专项602通过/1跳过、主工程211通过，均exit0；
250份原工作树历史XML未变。公开回归与发布核验详见机器证据，集合不累加。
历史put返回、journal写入/交付及Linux绑定仍未闭合，严格catalog保持false，
ResourceManager/GameState/真实Step未运行。详见
[报告](../agent-handoff/PURE_LINUX_BACKUP_QUEUE_SEMANTICS_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-backup-queue-semantics-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_QUEUE.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：loading 双槽真实磁盘事务

一次受控目标写入/恢复观测到4条同步commit完整链，返回均true；两槽最终XML
逐字节恢复，目标编辑与中间XML吻合，Backup均在双槽提交后触发。
251条原始记录留私有；新增SHA绑定离线审计器及28项synthetic审计器测试。
独立副本专项577通过/1跳过、主工程211通过、公开九组1104通过/42跳过，均exit0；
原工作树250份XML保持不变，集合不累加；完整发布检查见本轮机器证据。
fsync为UNCOVERED，journal本窗口未观测、真实异步归因和跨进程恢复NOT_RUN；
引擎前后完整身份未采集，Linux Backup binding和严格catalog仍BLOCKED。
未推进ResourceManager/GameState/Step、训练或性能测试。详见
[报告](../agent-handoff/PURE_LINUX_LOADING_DISK_TRANSACTION_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-loading-disk-transaction-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_LOADING_DISK.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：loading 路径映射与计数作用域修复

旧观察器对匹配的store入口没有建立线程作用域，内部编码键及无参commit可能被过滤；
旧fixture预期exit1复现，新8个JS mock与10个Python恢复用例通过。
修复后真实只读轮50条记录，getKeyValue/c.a.j/contains分别进入1/2/2次，无观察异常。
GameApp.c/o/p分别指向localPrefs/storage/storage_new；字面键false不证明编码键不存在，
恢复后的文件哈希相同也不排除中间落盘。旧记录保持原字节。
专项549通过/1跳过、主工程211通过；修复后store、跨进程恢复及真实异步归因NOT_RUN。
严格catalog仍BLOCKED，ResourceManager/GameState/Step与训练未启动。
详见[报告](../agent-handoff/PURE_LINUX_LOADING_STORE_PATHS_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-loading-store-paths-evidence-20261028.json)及
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_LOADING_SCOPE.md)。以下保留历史记录。

## 2026-10-28 E2.1-N：受控 loading 写入事务

隔离 Android 30 ARM64 AVD 上完成一次真实配置写入并恢复原值；观察到客户端
`requestBackup`、Binder `dataChanged`、服务端异步 `dataChangedImpl` 与 job 调度各两次。
pending 队列原已包含目标包，未将队列数量变化误作 journal 证据。跨进程磁盘提交、
journal、双槽身份和 Backup 交付仍 `UNRESOLVED`，严格 `CATALOG_READY_PASS=false`，
ResourceManager/GameState/真实 Step 为 `NOT_RUN`。全量专项回归 `531 passed, 1 skipped`，
exit 0；工程回归不替代原生生命周期证据。详见[回执](../agent-handoff/CODEX_REPLY_2026-10-28_LOADING_STORE.md)、
[报告](../agent-handoff/PURE_LINUX_LOADING_STORE_TRANSACTION_2026-10-28.md)和
[机器证据](../agent-handoff/pure-linux-loading-store-transaction-evidence-20261028.json)。

## 2026-10-28 E2.1-N：Backup callback 服务端语义调查

对历史 ART trace 在独立目录重分析：835,263条记录，23个 `requestBackup` scope、
2个线程，均正常返回；参数/返回值未采集。匹配 Android 30 服务端 DEX 的11个方法
为STATIC_FOUND，调用边覆盖用户/权限检查、handler入队、journal和job调度；不是
服务端动态投递证据。客户端Binder事务静态 flags=0。服务交付与callback结果仍UNRESOLVED，
严格catalog门=false、ResourceManager BLOCKED、GameState/真实Step NOT_RUN。
49项服务端摘要、ART trace与方法审计专项测试通过。详见[报告](../agent-handoff/PURE_LINUX_BACKUP_SEMANTICS_2026-10-28.md)、
[机器证据](../agent-handoff/pure-linux-backup-semantics-evidence-20261028.json)和
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_SEMANTICS.md)。

## 2026-10-28 E2.1-N：双槽配置接入与 backup 动态边界

v116 独立后端两 child 各 48 条历史参考一致、两槽持久化及重开通过，未加载 libg。
v117/v118 四个真实 libg child 各两次真实 commit 后因候选 backup callback 未闭合主动 exit96；
wrapper exit1，ready 仅 0，表加载未完成。不是原版 BackupManager 的动态失败证据。
v121 两个 NDK checkService 查询在 /dev/binder ENOENT 后 SIGABRT(-6)，未返回；服务状态未知。
各 187 输入不变；四个 libg child 各 188 实际映射核验，资源清单不是新的 9,829 资产全量对照。
v122 隔离专项 482 通过/1 跳过、主工程 211 通过、公开九组 1,104 通过/42 跳过，入口均 exit0。
248 份 tracked 历史 XML 保留；失败记录、当前初始化身份及完整异常域缺口保留。
严格 catalog/manager 仍 BLOCKED，GameState、真正 Step、完整局/no-VM/10×/训练 NOT_RUN。
详见[报告](../agent-handoff/PURE_LINUX_PREFERENCE_BACKEND_2026-10-28.md)、
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_BACKEND.md)及
[机器证据](../agent-handoff/pure-linux-preference-backend-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置文件原语与Android30静态证据

两个新ARM64/Bionic子进程各19组/444项文件检查通过，187受保护输入未变；
覆盖分步故障、bak恢复、进程退出、exec重启、容量canary及权限/所有权。
隔离专项409通过/1跳过、主工程211通过、公开九组1,104通过/42跳过；集合不累加。
10个真实平台方法STATIC_FOUND，发现FileUtils.sync及删除bak的返回值未使用；
backup实际服务绑定未知，静态空服务分支不等于当前运行证据。
文件测试采用合成字节，未接入libg；XML/map、当前身份、listener/queue及backup契约未闭合。
严格catalog仍false，ResourceManager BLOCKED，GameState/真实Step及完整局NOT_RUN。
命令、独立回归及输入/输出SHA见[报告](../agent-handoff/PURE_LINUX_PREFERENCE_FILES_2026-10-28.md)
和[机器证据](../agent-handoff/pure-linux-preference-files-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：历史padding序列与store0参考

v106两份历史原版归档各两个新ARM64/Bionic子进程，四条返回码/长度/字节/canary全匹配：
成功16、失败输出0、成功32、成功16。expected只取原版result，缺原始ciphertext的归档显式拒收。
XML原字节恢复为[true,false]，解析条目恢复；不宣称全部字节恢复。
v107从原版历史身份记录绑定store0，两份参考各两个child、每child30条正常KEY/ENC/DEC全匹配。
各187输入未变、9实际映射核验，均未重新加载libg。保留首次缺input失败、store参数遮蔽测试失败
及宿主OpenSSL3异常后字节差异。当前身份、Java字符串全域、storage/commit/backup仍未闭合，
严格catalog仍false、ResourceManager BLOCKED，GameState/真实Step/完整局/Android逐tick对照NOT_RUN。
v108独立副本专项378通过/1跳过、主工程211通过、公开九组1,104通过/42跳过，均exit0；
集合不累加，248份工作树历史XML与HEAD逐字节一致。
实际命令与本轮隔离回归见[报告](../agent-handoff/PURE_LINUX_PREFERENCE_SEQUENCE_2026-10-28.md)、
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_SEQUENCE.md)及
[机器证据](../agent-handoff/pure-linux-preference-sequence-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：持久Cipher及历史原版正常字节

新增SHA绑定的p身份静态提取、三持久EVP上下文及历史original参考胶囊工具。
v97两child退出0但collector因逻辑linker路径求SHA退出1，失败保留；v99修复实际映射后
两child/187项输入/9映射通过。v101两首块、20边界组合及错误序列为候选观察，
v102每child两条XML加密键匹配；v104两个新ARM64/Bionic子进程各30条历史正常路径
ECB/CBC参考逐字节一致、187项未变。expected仅来自历史original字段与已知输入，
不来自native候选；不等于当前loading键及完整初始化等价。

v105隔离专项304通过/1条件跳过、主工程211通过、公开九组1,104通过/42跳过/零失败错误。
248份工作树已跟踪XML与HEAD一致，未覆盖历史输出。当前身份、异常后状态及真实
storage/backup UNRESOLVED，CATALOG_READY_PASS=false、manager BLOCKED；
GameState、空场/reset、真实Step、动作、完整战斗及Android逐tick对照NOT_RUN。
本阶段未重新加载libg，未启动Android、训练或10×测试。证据边界与实际命令见
[报告](../agent-handoff/PURE_LINUX_PREFERENCE_CIPHER_2026-10-28.md)、
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_CIPHER.md)及
[机器证据](../agent-handoff/pure-linux-preference-cipher-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：初始化图与配置控制流差分

真实当前/历史DEX的有界初始化图142节点、15分支相同，8个配置方法摘要相同，
完整onCreate不同及历史失败保留。恢复显式C后端上的分步读写/commit/异常契约。
v89首次4,522例有252差异；修正Python adapter的解密异常映射后，v90/v92及新v93
当前/历史各4,522例零差异。外部codec/storage/backup为共享合成边界，不代表ART等价。
v91两个ARM64/Bionic候选子进程exit0，15故障边界/8场景组、187项输入未变，未加载libg。
历史PN持久Cipher状态及限定串行证据只读审查，不冒作当前loading键动态验证。
密码、平台存储/backup及传递初始化仍UNRESOLVED，严格catalog/manager门BLOCKED，
GameState/Step/完整局NOT_RUN。命令、SHA及独立工程回归见
[报告](../agent-handoff/PURE_LINUX_PREFERENCE_SEMANTICS_2026-10-28.md)、
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_SEMANTICS.md)及
[机器证据](../agent-handoff/pure-linux-preference-semantics-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置候选租约与初始化契约

两个新Ubuntu ARM64/Bionic实验各11条进程回执符合预期：五竞争者被拒绝，正常释放及
杀死本轮holder后恢复成功；各187个输入SHA不变。仅运行配置候选，没有重新加载libg。
静态方法审计8/8相同；含onCreate的9方法审计exit2，配置对象前缀的数组载荷相同，
条件分支目标仍待闭合。新增工具保留寄存器/异常表及真实数组字节，未覆盖载荷显式拒绝。
专项181通过/1条件跳过；原始失败、历史报告与XML保留。Android加密、分步commit/failure、
初始化身份及真实backup副作用仍UNRESOLVED，CATALOG_READY_PASS=false、manager BLOCKED，
GameState/Step/完整局NOT_RUN。完整命令、SHA和发布回归见
[报告](../agent-handoff/PURE_LINUX_CONFIG_CONTRACT_2026-10-28.md)与
[回执](../agent-handoff/CODEX_REPLY_2026-10-28_CONFIG_CONTRACT.md)。以下为历史状态。

## 2026-10-28 E2.1-N：自然 catalog 加载与单键配置候选

真实Ubuntu ARM64 v78/v79各wrapper0/child0；loader53步到终态9，ready自然0→1，
162表/24,353行，布局SHA一致。247个APK文件完整read、131个update文件完整只读mmap，
378个实际打开路径前后manifest/SHA一致；mmap不证明逐字节消费，布局不证明cell内容等价。
两次有序追踪各确认247次同路径update ENOENT后APK打开、131次update exists成功后打开，
unknown路径0、资源fd残留0。证据仅覆盖实际路径，不代表全部namespace/fallback异常语义。

纠正下方历史归因：原故障是PreferenceWrite→storeKeyValue→JNIEnv→空JavaVM，
不是已证明的日志桥，AttachCurrentThread尚未执行。新增单键native持久化候选，
真实六进程恢复/拒绝用例符合预期；Android加密、commit/failure和BackupManager外部副作用
仍UNRESOLVED，因此CATALOG_READY_PASS=false，ResourceManager整体门BLOCKED。
GameState/empty/reset/Step/动作/完整局/Android对照NOT_RUN，未启动训练、10×或无VM验收。

隔离专项157通过/1平台条件跳过、主工程211通过、公开入口exit0/9组XML零失败错误；
trace行序诊断后专项复跑一致。旧报告、旧失败和历史XML原字节保留。
命令、退出码、输入输出及私有日志SHA见[新报告](../agent-handoff/PURE_LINUX_CATALOG_CONFIG_2026-10-28.md)
和[证据](../agent-handoff/pure-linux-catalog-config-evidence-20261028.json)。以下为历史状态。

## 2026-10-28 E2.1-N：Provider / catalog 前置依赖

data-only/update-only/both三个独立原生注册及释放通过；四次settings/version真实资源
读取wrapper0/child0，完整字节SHA、length/read/EOF/seek/close/backend finish均通过。
两次catalog list158/preload368和分批load返回，每次369个资源只读mmap共6,071,750字节，
暂存SHA一致；未hash映射内存，ready始终0，未取得CATALOG_READY_PASS或RESOURCE_MANAGER_PASS。
自然loader进入native log→Java logger→JNIEnv获取，在空JavaVM指针处SIGSEGV；
未实际调用AttachCurrentThread。wrapper/GDB1与inferior exit_code=null分别记录。
旧路径child60与旧probe不识别APK虚表child61保留；未跳日志或假造VM。
专项108通过/1平台条件跳过，主工程211通过；公开9组XML零失败零错误，最终wrapper退出码
未持久化而保持null。测试在隔离副本运行，历史XML保留。真实update优先级/fallback尚未验证。
GameState/empty/reset/Step/完整局NOT_RUN，整体BLOCKED，无训练或10×结论。详见
[报告](../agent-handoff/PURE_LINUX_PROVIDER_CATALOG_2026-10-28.md)及机器证据。

## 2026-10-27 E2.1-N：原生初始化对象图限定推进

真实Ubuntu/aarch64中libg753/753 constructor调用返回，全体1070/1070；
content bootstrap构造root/context，filesystem bootstrap分配392字节对象，均exit0。
取得CONSTRUCTOR_TRACE_PASS及CONTENT_CONTEXT_PASS；ready=0，未登记MANAGER_INIT_PASS。
27个post-load断点两正对照各1，其余已知JNI/Asset地址0，仅覆盖明确地址和分配窗口。
属性路径及logdw访问已真实出现，不作Android-free或no-JavaVM生命周期结论。
v3 tagged-pointer观测错误、v6断点撞guard、REA超时、写盘失败及恢复证据保留。
42项pure_linux工程专项通过；GameState/empty/reset/Step仍NOT_RUN，整体BLOCKED。
首个前置依赖是provider注册与catalog真实加载契约，详见
[报告](../agent-handoff/PURE_LINUX_NATIVE_INIT_2026-10-27.md)及机器可读证据。

## 2026-10-27 用户 APK 续验：真实原版库 LOAD_ONLY

新提供 APK 的8个 ARM64 库和9,829个 assets（849,482,445 bytes）逐项 SHA 匹配；
runtime214/214、update990/990、fingerprint匹配。Java/Android资源及整包SHA存在差异，
因此单独记录输入身份，保留旧清单和gate。在真实Ubuntu ARM64 guest的新目录实际
dlopen(RTLD_NOW|RTLD_LOCAL)/dlclose真实libg，进程exit0；187个暂存输入前后SHA相同。
只有LOAD_ONLY，没有战斗初始化、动作/tick/快照/终局或无JavaVM冷启动结论。
详见[报告](../agent-handoff/PURE_LINUX_USER_APK_LOAD_2026-10-27.md)。

## 2026-10-27 E2.1-N 续验：真实 guest 合成 ABI 通过

现有 Fusion Ubuntu 22.04.5/Linux 6.8.0-138-generic guest 实测 aarch64。首次自有 smoke
因 raw clone 的 TLS 参数位置错误 SIGSEGV（程序 -11、runner 1）；失败原样保留。
修复后 v4 SHA 1969edf1…9b232，真实 guest 八项检查及 canary 通过，正常退出 0、
指定退出 23，runner 0，获得 PURE_LINUX_SMOKE_PASS。Bionic 探针和 --list 均 exit 0，
实际 libc/libdl 来自本实验隔离目录；未加载真实 libg。

新增只读版本符号和 relocation 审计：185 个 ARM64 库、473 导入，469 有静态候选，
4 个 unresolved weak ZSTD trace 符号保持未知；753 个 init_array 槽未运行。
专项 29 项通过。父提交隔离副本加新增 loader 的主工程 211 项通过21.79秒，公开入口
exit0，主组794/27skip 48.40秒，其他23、82、15/3skip、25/4skip、31/3skip、31/2skip、
38/3skip、65，各集合不累加；最后汇编修复由实际 guest 重验。

guest 环境阻塞解除，首个真实加载门仍为锁定原 APK MISSING_INPUT（app 8/9），
不是引擎失败。真实 libg LOAD_ONLY、生命周期、no-VM、Android-free 和10×均未取得。
完整命令、SHA、私有日志标识见[续验报告](../agent-handoff/PURE_LINUX_GUEST_ABI_2026-10-27.md)
及[证据](../agent-handoff/pure-linux-guest-abi-evidence-20261027.json)。以下为历史状态。

## 2026-10-27 E2.1-N：ARM64 Linux ABI 实验准备与静态审计

仅任务分支，源基线82b94be；不修改main或冻结部署。Darwin arm64/macOS15.6，
Fusion运行实例0，已有VM存在挂起状态而保持原状；未取得live guest uname/网络。
动态门BLOCKED_ENVIRONMENT，未执行Linux ELF或原版加载，无PURE_LINUX_SMOKE_PASS。

真实匹配libg审计：12个DT_NEEDED、480个符号槽、473个未定义符号、JNI_OnLoad及4个Java导出。
PT_DYNAMIC恢复符号，纠正旧“缺节表”为有5个section header但无可用动态符号节表。
214/214运行文件、990/990资源匹配，app8/9缺原APK；候选整包SHA不匹配锁定身份。
ABI筛选后检查185个ARM64候选库；ARM32同名库不再误报歧义，namespace/版本/relocation仍未知。
GameStateTick为计数读取器，GameStateStep才是推进入口候选；独立初始化对象图未闭合。

新增ELF/manifest审计、独立ARM64 syscall/thread/TLS/IPC/signal探针、Bionic探针及执行守门器。
15项合成/工程测试通过，两种探针以-Werror交叉构建exit0；实际Darwin守门exit2且未执行ELF。
隔离起始tracked snapshot的主工程211通过21.64秒；公开入口exit0，主组794通过/27跳过49.09秒，
另有23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、65项，各组不相加。
新增工具由独立15项覆盖。首次解压TypeError、旧ABI误分类和旧构建保留，不覆写失败输出。

工程实现/构建不代表Bionic运行、LOAD_ONLY、无VM原生生命周期或10×通过。详见
[阶段回执](../agent-handoff/CODEX_REPLY_2026-10-26_PURE_LINUX.md)及机器可读dependency-report.json。

## 2026-10-25 整轮执行、独立回收与恢复

完整公开入口退出0：主回归794通过/27跳过108.55秒，阈值23通过9.58秒，
恢复交接82通过187.71秒，位置15通过/3跳过0.34秒，执行链25通过/4跳过
1.76秒，虚分派31通过/3跳过1.39秒，预约31通过/2跳过1.59秒，坐标38通过/
3跳过3.90秒；预检+执行协调65通过41.11秒、零跳过。2329项源码清单通过，
凭据扫描约39MB零发现。各测试范围不累加，公开依赖跳过不替代原生验收。

collection-coordinator-v1新增显式执行入口，固定prepared及父选优报告原始SHA，
重验所有冻结输入后顺序执行worker；每局独立归档验收，整轮最后复核全部采集
树才原子提交complete。恢复重复验收成功局而不重复采集；活worker、未知启动
身份及失败attempt停止并保留证据。主机/boot_id、PID/start_ticks与排他锁绑定。

初始执行29项通过29.65秒；补测后预检+执行+worker+验收器联合117项通过
123.45秒、零跳过，含首次专项，不累加。主工程211项通过54.09秒。测试子程序、
模型和对局为fixture，进程身份模拟；不替代Linux原生执行、真实round2、教师
标签重算、三次重试调度、整轮合并训练交接或连续补充梯度恢复。

Full128快照1792917635.1268315在线，185组/370根/47360模拟已提交、持久
tick5982、tick5990玩家0继续；terminal仍运行，尚无完整搜索局或真实terminal
准入。G0/G1及34/43保持开放，G2–G8、正式训练和五机放量未启动。

## 2026-10-25 整轮补充采集协调预检

collection-coordinator-v1将现有全历史/种子来源登记、部署和请求验收接到整轮
准备入口。要求任务恰好完整覆盖、共享输入一致、输出独立；逐任务在隔离子
进程运行现有collector预检，核验模型/计划/登记身份及工程范围。完整父数据树、
权重、配置、部署与来源文件在预检前后重验，变化停止并保留现场。

32项专项通过5.61秒，联合128项通过92.03秒，零跳过；集合有包含关系，
不相加。工程专项模型、父数据及子程序为fixture，实际独立子进程只证明协调
顺序和拒收，未证明真实模型预检或原生round2。prepared.json最后原子发布，
只含准备结果和既有worker命令，不执行采集。完整调度、回收、重试及训练衔接
仍待完成，不把准备标记当作原生终局或正式G2准入。

首次公开检查新专项32项setup error，源于共享fixture间接读取已排除的私有
manifest；原字节报告为该版tests-public-initial.xml。改用显式合成manifest，
真实native_plan和历史归档审计不变，未复制附件或跳过专项。修复后源联合113项
通过119.63秒，零跳过；完整公开入口退出0，新专项32通过5.33秒、零跳过。

公开主回归794通过/27跳过90.88秒；独立阈值23通过11.19秒、恢复交接82通过
162.07秒、位置15通过/3跳过0.36秒、执行链25通过/4跳过2.12秒、虚分派31通过/
3跳过1.54秒、预约映射31通过/2跳过1.47秒、坐标38通过/3跳过3.45秒。
主工程211通过51.99秒，范围不累加；2325项源码清单通过，凭据扫描零发现。

Full128快照1792916545.8936303已提交184组/368根/47104模拟，持久tick5978，
tick5982玩家0搜索中；terminal第18步完成、第19步开始，仍无terminal准入。
坐标阶段188预约核验结果同步补入闭合清单。G0/G1与34/43保持开放，G2–G8、
正式训练及五机放量未启动，冻结部署保持。

## 2026-10-25 模型格点到原生执行坐标限定验收

command-coordinates-v1独立v5插件新增X2，不增加观察点；核对命令对象+0x28
加载横纵坐标到helper期间未覆盖。独立重算玩家旋转、placement偏移、舍入与
边界裁剪，先比较HandAction，再逐预约对象关联实际X1/X2。

62份归档实际独立验收：三组各213组/426次决策、20个周期观察边界与tick5897
玩家0胜终局一致。188预约坐标全部一致，双方96/92个，33个带偏移、14种实际
坐标；68个tick双方同时执行、20个对象跨tick复用，父映射/虚分派及所属清理通过。
三组214.739456517/245.000375508/330.437957781秒，日志221145字节/1736条；
单次正确性对照不作性能结论。公开摘要绑定来源与验收器SHA，私有原始审计保留。

最终41项专项通过、零跳过4.94秒，包括实际归档重验及3,456种冻结转换对照。
首次review测试误用不存在的类名失败原字节保留；修正后隔离进程并核对冻结路径。
公开版缺私有附件显式跳过三项。内部卡牌、完整mask、一般合法拒绝、全部随机
机制及Full128终局仍待验，G0/G1与34/43保持开放，不启动G2–G8或五机放量。

Full128快照1792915142：183组/366根/46848模拟已提交，tick5978玩家0继续，
worker与双引擎在线、未终局；terminal第18步验证最近到192/358行，尚无terminal
或round2准入。主工程211通过53.59秒；公开794通过/27跳过100.74秒，独立
阈值23通过、恢复交接82通过、位置15通过/3跳过、执行链25通过/4跳过、虚分派
31通过/3跳过、预约映射31通过/2跳过、坐标38通过/3跳过；测试集合不相加。

最终导出后坐标公开复核38通过/3跳过4.94秒；2317项源码清单核对通过，
凭据扫描零发现。三项公开跳过分别缺固定库、旧私有轨迹与本次私有归档，
不替代上述原生41项零跳过结果；新增文件另执行暂存区空白检查。

## 2026-10-25 逐预约原生对象完整轨迹限定验收

reservation-map-v1逐次记录同步入队日志字节范围，关联预约、构造与同tick真实
ct86执行对象，不按双方执行顺序猜测。62份归档独立验收：三组各213组/426次
决策、20个周期观察边界与tick5897玩家0胜终局一致。188预约全部关联，双方
96/92个，68个双方同时执行tick，共20个对象跨tick复用；构造/执行全部覆盖，
回执、到期及最终结算一致，父虚分派检查与所属引擎清理、端口释放通过。

日志191633字节/1736条；三组分别215.234034/240.710302/329.171402秒。本轮
是正确性对照，不作为性能因果结论。最终33项专项通过、零跳过2.22秒，包含
真实完整归档重验及旧成功父缺逐次入队区间时拒收。公开摘要绑定launch/receipt/
verification及验收器身份；私有地址、寄存器、原始审计与第三方库不公开。

只证明本轨迹逐预约对象关联，不证明原生命令内部卡牌/坐标字段或坐标转换；
一般合法拒绝、完整mask、全部随机机制、完整搜索局及原生terminal仍待验。
Full128快照1792913752提交182组/364根/46592模拟，tick5970玩家0搜索中、
未终局；terminal第17步完成，第18步验证中。G0/G1及34/43保持开放，G2–G8、
正式训练和五机放量未启动，运行中的冻结部署保持原样。

公开回归794通过/27跳过47.37秒；阈值23通过4.38秒、恢复交接82通过81.42秒、
位置15通过/3跳过0.13秒、执行链25通过/4跳过0.77秒、虚分派31通过/3跳过
0.76秒、预约映射31通过/2跳过0.75秒；主工程211通过22.14秒。集合不相加。
最终导出后预约映射复核31通过/2跳过1.16秒；2306项源码清单核对通过，凭据
扫描零发现。公开副本的两项私有归档测试跳过不替代原生33项零跳过结果。

## 2026-10-25 虚分派实际目标完整轨迹限定验收

dispatch-command-v1独立v4日志记录X8，不复用X1语义；增加调用前与返回后指令
观察，共17点。62份归档独立验收通过：三组各213组/426次决策、20个周期观察
边界和tick5897玩家0胜终局一致。188次实际目标与ct86入口、对象、参数、LR及
返回后W0逐次一致，均返回0，原有执行链及随机入口正向对照完整，所属清理通过。

原始日志191633字节、1736条记录已重算，完整终局后监督封存；baseline/upgraded/
plugin耗时216.410744/244.763152/328.065243秒，插件127.833810秒就绪。这是一轮
正确性对照，不作为性能因果结论。34项最终专项通过3.33秒、零跳过，含本次真实
归档重验和旧v3真实归档缺X8时拒收。原始私有审计保留，不随公开副本发布。

本轨迹虚分派调用点已证明，逐预约映射、一般合法拒绝可达性、完整mask及全部
随机覆盖仍待验。Full128快照1792911277提交180组/360根/46080模拟，tick5906
玩家0搜索中；terminal已完成第16步、开始第17步，真实终止及round2尚待验。
G0/G1与34/43保持开放，G2–G8、正式训练和五机放量未启动，冻结部署保持原样。

本轮公开794通过/27跳过47.56秒；独立阈值23通过4.36秒、恢复交接82通过80.81秒、
位置15通过/3跳过0.12秒、旧执行链25通过/4跳过0.74秒、新虚分派31通过/3跳过
0.74秒；主工程211通过23.09秒。缺私有附件或CPU依赖的测试显式跳过，各集合不相加。
2294项源码清单通过，凭据扫描零发现；新增文件另纳入暂存区空白检查。

## 2026-10-25 原生动作执行链限定验收

live-command-v1独立第三次实测的62份归档通过内容验收。baseline/upgraded/plugin
三组各213组/426次决策及20个周期观察边界一致，同在tick5897原生终局。
工厂分支、构造、ct86执行器进出、位置helper进出及返回后指令各188次，
执行器对象均见于构造，构造LR对应工厂，执行器LR对应0xd2f838调用点。
执行器与helper返回码均0，报告正常执行188次；尚未逐预约关联对象身份。
reset正向对照、完整终局后监督封存及所属引擎清理通过。

两次启动失败保留且由独立验收器实际拒收；ARM前新增点直接返回的过滤版
127.644751秒就绪、327.182504秒完成。未改guest或关闭alarm；单次成功不作
独立性能因果结论。29项专项通过2.34秒、零跳过，包含实际归档重算。
公开版缺第三方库和私有原始归档时显式跳过；原始地址/寄存器及权重不公开。

该结果不证明虚分派目标寄存器、一般合法拒绝可达性、完整mask或全部随机覆盖。
Full128快照1792910104已提交179组/358根/45824有效模拟，tick5874玩家1继续；
terminal恢复训练已到第17步，原生terminal与round2仍待验。G0/G1与34/43不变，
G2–G8、正式训练及五机放量未启动。详见该版EVIDENCE.md和native-evidence-summary.json。

本轮公开回归794通过/27跳过，独立阈值23通过、恢复交接82通过、位置15通过/
3跳过，主工程211通过；最终公开执行链25通过/4项缺私有附件跳过1.12秒。
2284项源码清单通过，凭据扫描零发现。测试集合存在重叠，不相加。

## 2026-10-25 位置拒绝条件的原始指令差分

position-guards-v1在独立Unicorn 2.1.4 AArch64 CPU执行固定SHA库的原始81条
指令，14,676个确定性合成结构体样例全部一致，81/81条指令与返回码0..5覆盖，
耗时2.245471秒。18项专项通过；整数界证书另证明除500等同于全部signed32
输入的向零截断，不称CPU穷举。ct86调用点确认位置非零返回加14，对应15..19。

六个直接调用及一个虚调用显式stub，核对参数、破坏caller-saved和NZCV，核对
callee-saved/SP/LR及栈写入范围。未执行terrain函数体，不证明真实游戏可达性、
live dispatch、其它ct86条件或完整mask到期充分性，不增加34/43或关闭G0/G1。
依赖仅独立安装，第三方库和wheel不公开；源码、报告、依赖身份及局限详见
该版CONTRACT.md、EVIDENCE.md、guard-report.json。

Full128快照1792907076提交177组/354根/45312有效模拟，tick5864继续、未终局；
terminal恢复仍运行。G2–G8、正式迁移、训练和五机放量未启动，冻结部署保持。
发布复核：公开794通过/27跳过48.29秒，独立阈值23通过4.32秒、恢复与交接82
通过80.20秒，公开位置专项15通过/3项CPU依赖或私有库缺失跳过0.13秒；主工程
211通过22.74秒，2264项源码清单通过且凭据扫描零发现。各集合不累加。

## 2026-10-25 最早合法延迟原生诊断收尾

affordability-boundaries-v1从真实mask逐卡/格重算最早合法延迟，同阈值取首尾格，
测试阈值和合法下一档。tick90/110/2598/5602共37组、74次快照续演通过实际
独立内容验收；其中36组覆盖delay2/4/15/18/25及下一档，一组双方WAIT基线。
合法拒绝0组、单方就绪0组，完整边界续演一致、快照释放及所属引擎清理通过。
probe耗时224.412743秒，六份归档SHA及字节数核对一致；公开摘要固定launch、
receipt、验收器和verification身份，原始审计及第三方引擎不公开。

23项工程专项通过4.279秒，公开独立进程23通过4.443秒；完整fixture及重签
语义篡改只作工程测试，不冒充原生证据。有限负结果不证明全格子/全轨迹不可达，
不替代一般pending信念重建、Full128或跨进程恢复。阈值也不直接归因为圣水。
详见该版EVIDENCE.md和native-evidence-summary.json。

Full128快照1792905521已提交176组/352根/45056模拟，tick5804玩家0搜索中，
worker和双引擎存活、未终局；terminal恢复仍待验，G0/G1和34/43保持开放。
G2–G8、正式迁移、训练和五机放量未启动，运行中的冻结部署原样保持。
本轮公开794通过/27项缺私有附件跳过47.947秒，独立恢复与交接82通过82.091秒，
主工程211通过22.08秒；测试集合不相加。

## 2026-10-25 全局快照原生搜索验收与terminal恢复交接

global-search-v1实际独立验收同一冻结任务的369份归档：四根各128有效模拟、
两对真实隐藏干预结果一致，完整native+global-MT逐根摘要恢复、1483次创建与
释放、551次恢复及所属引擎清理齐全。probe耗时2078.438453秒，公开摘要绑定
launch/receipt/验收SHA。首次CLI误载主工程包而拒收，新进程先固定history-v2
后运行原字节验收器通过；保留失败摘要，新增verify_isolated.py入口。
不替代完整搜索局、一般pending/fallback或全部随机覆盖。

retry-collection-bridge-v1先验停止的terminal恢复父，再确定性重建补充采集包，
继承冻结训练字节。拒收篡改、重封装、路径别名、活动租约及损坏写出，最后原子
发布handoff。最终联合82项通过79.85秒，含39项交接；成功父为显式mock，真实
无终止回执父在输出前拒收。首次目录夹具错误和后续各轮结果分别保留。
真实terminal与round2仍待验，不自动开始补充采集。

Full128快照1792904194：175组/350根/44800模拟已提交，tick5744玩家0搜索中，
未终局；terminal恢复仍运行。G0/G1、34/43及正式阶段准入不变。
发布复核：公开794通过/27项缺私有附件跳过49.50秒，独立恢复与交接82通过
80.95秒，主工程211通过23.02秒；2244项清单通过，凭据扫描零发现，集合不相加。

## 2026-10-25 terminal超时独立恢复与后台搜索

selection-timeout-v1完成失败父内容重验、独立有界恢复、严格身份派发、稳定
归档和terminal独立验收工具。新增17项+既有26项联合43项通过79.656秒，
小模型第9→24步恢复与连续训练完整状态一致，真实子进程fixture验证参数贯通、
父字节保持及失败隔离；模拟传输不算原生成功。两次测试构造失败原字节保留。

真实旧选优28800秒超时exit124，第15步/epoch7/offset256/2720行状态已核验。
独立86400秒续跑在.35 GPU1启动，worker73377/start49307405，子进程
73450/start49307994；快照1792903002活跃，尚无terminal通过。旧失败保留。
Full128快照1792902982为174组/348根/44544有效模拟、tick5728玩家0搜索中；
独立全局MT搜索快照1792902971完成1/4根。完整局、四根验收与后续round2待验。
G0/G1开放、34/43不变，G2–G8及正式训练未启动；详见该版本EVIDENCE.md。
发布复核：公开794通过/27跳过49.61秒，独立恢复回归43通过79.76秒，主工程
211通过22.74秒；2230项清单通过，凭据扫描零发现。各集合不相加。

## 2026-10-24 直接随机初始化参数的剩余清单

rng-callsite-audit-v1实际读取固定SHA的私有库，重算父证据和18处直接引用，
输出完整局部片段及X0/W1来源表达式。11处栈相对、6处分配器返回、1处传入；
operator new的PLT/导入另核验。25项专项、与shuffle/alias/seed联合130项
通过（10.04秒），含固定库报告重算及寄存器/控制转移拒收。首次两项失败保留。
未知种子及被调函数副作用不作推断，地址分类不证明持久性或快照完整性。
五机SSH仍超时，原任务待核验，G0/G1及34/43不变。详见该版本EVIDENCE.md。
公开794通过/27项缺私有附件跳过（104.18秒），2220项清单通过，凭据扫描零发现。

## 2026-10-24 全局随机快照与搜索集成工程

global-search-v1保留冻结部署，在独立入口接入全局MT修复引擎。快照完整摘要、
generation/configRevision/stateEpoch及句柄生命周期逐次核对，每根关联独立日志
区间与根摘要；拒绝累计恢复次数替代逐根证据。审计不进入FAIR、模型或节点身份。
38项专项、与已有provider/snapshot联合139项通过（7.81秒）；包括真实父归档
重验及实际构建载荷校验，合成传输测试单独说明，未冒充新原生游戏证据。
353项冻结载荷已准备；计划四根各128模拟。五机SSH TCP22均超时，原Full128及
terminal当前状态待核验，不据失联重启。原生新搜索待执行，整体G0/G1与34/43
不变，G2–G8、正式训练和放量未启动。详见该版本CONTRACT.md和EVIDENCE.md。
发布复核：公开770通过/26项缺私有附件跳过（95.76秒），主工程211通过
（51.03秒），2212项源码清单通过，凭据扫描零发现。准备包本机再次核验一致，
实际launch在SSH连接阶段超时，未生成launch记录，不当作原生已启动。

## 2026-10-23 独立低开销随机入口插件

plugin-runtime-v1完成官方QEMU10.0.13 user-only及plugins构建，四线程、198.574秒，
33份构建归档内容核验通过。依赖仅独立解包，保留前三次构建失败与旧日志追踪
第五次SIGXFSZ失败；不升级系统或修改运行中冻结部署。AArch64合成ELF两条执行
回调的PC/参数/返回地址及END校验通过，是接口检查，不冒充原生游戏证据。
修复vCPU退出清理、块内PC误判，保留原始PC并使用实际指令回调地址。v3在reset
前绑定本次进程实际库地址；监督封存要求完整终局与所属进程清理，不补写END。
入口及块内合成程序检查通过，后者实际观测PC差异且正确读取前置MOV的寄存器值。
五次真实失败保留；第六次三组完整局通过独立限定验收，62份归档及清理通过。
每组5807步、tick5897终局、213组决策共426行，双方ACT执行96/92次；全部决策、
回执、终局、时钟及20个周期/终局观察摘要一致。日志4085字节、44条入口记录；
reset两类正向对照齐全，战斗段只命中manager xorshift_save20次。缺END通过明确
监督封存验证，不补尾标，不外推其他算法、所有seed/动作、启动前调用或全随机覆盖。
阶段耗时215.546/244.993/326.562秒，probe167.694/188.484/190.227秒，仅一次测量，
不是吞吐或搜索倍率。公开native-evidence-summary.json不含原始地址和寄存器。
联合139通过（8.51秒）、公开734通过/24项缺私有附件跳过（110.70秒），
主工程211通过（53.52秒），2202项源码清单通过、凭据扫描零发现；集合不相加。
Full128快照1792751687提交133组/266根/34048模拟，未终局；terminal仍运行。
G0/G1与34/43保持，G2–G8、正式训练与放量未启动。

## 2026-10-23 随机入口执行追踪工程与原生阻断

random-execution-v1新增校准/追踪、QEMU/service/探针/父证明身份固定与原始日志
独立重算。核对双方合法动作、同tick预约、完整结算时钟及原生终局；要求reset
正向对照，拒收空日志、执行头/CPU PC不符及QEMU未执行TB记录。61项专项通过。
成功证据结构测试为显式fixture，真实第四次失败归档经实际验收器拒收。

四次原生尝试分别为规则绑定缺项、两次140秒启动超时，以及独立延至600秒后
启动子进程仍以SIGALRM退出（实际232.595秒）。日志均未形成有效证据；失败
SHA摘要公开，原始审计私有保留。后续优先采用低开销或就绪后启用的追踪方式，
不继续盲目延长等待，不推断固定速猪随机入口不可达。

Full128快照1792744059：127组/254根/32512模拟提交，tick4604玩家0搜索中；
terminal选优快照1792743342仍运行。冻结部署、G0/G1及34/43保持，G2–G8未运行。
发布复核：2162项源码清单通过；公开回归663通过/17项缺私有附件跳过（47.74秒），
主工程211通过（23.99秒）、联合163通过（8.43秒），凭据扫描零发现；集合不相加。

## 2026-10-23 terminal选优到补充采集包交接

migration-integration-v1/build_collection_payload.py新增selection模式，显式固定
execution/completion SHA和父数据根；实际执行独立内容验收后继承原训练字节。
复核checkpoint及父证据未在验收后改变，handoff/payload通过原校验器重新读取。
35项专项8.82秒、联合78项58.28秒通过；成功交接使用mock验收器隔离测试，
真实不合格归档经实际验收器拒收，不宣称真实terminal已通过。
完整seed登记和collector预检仍必须；此入口只构建部署包，不启动原生对局。
真实terminal快照1792741007仍运行；Full128快照1792740718已提交124组/248根/
31744有效模拟，tick4482玩家1搜索中、未终局。G0/G1及34/43不变，G2–G8未运行。
后续快照1792741500：125组/250根/32000有效模拟提交，tick4516玩家1搜索中。
发布复核：2143项源码清单通过；公开回归603通过/16项缺私有库跳过（48.29秒），
主工程211通过（22.40秒），凭据扫描零发现；集合不相加。

## 2026-10-23 原生开局先验诊断完成

native-prior-sampling-v1/EVIDENCE.md与native-evidence-summary.json记录1024个
预注册seed主采样及第二新进程64个seed复测。17份归档、完整行数/来源/牌序、
跨进程可观察重复及所属引擎清理独立验收通过；两段7947.972344/504.683079秒。
21项事前卡方均未拒绝，最小p=0.07721489409，高于Bonferroni阈值0.01/21。
双方各观察到1006/1016种完整排列，原始审计私有保留。

结论为verified_diagnostic，不证明精确均匀、40320排列全支持、隐藏RNG一致、
历史条件后验或完整随机覆盖，不改变G0/G1、34/43及训练准入状态。
Full128快照1792738671：123组/246根/31488模拟提交，tick4470玩家1搜索中，
无失败、未终局；terminal选优仍运行，冻结部署保持原样。
本轮2140项源码清单通过，公开回归584通过/16项缺私有库跳过（49.01秒），
主工程211通过（24.21秒），
凭据扫描零发现；测试集合不相加。

## 2026-10-23 单方就绪边界条件分析

boundary-reachability-v1/CONTRACT.md给出正常成功轨迹保持同步的归纳前提；
audit_boundaries.py运行真实history-v2 Runtime/Clock，对31×31种时间类别与
最早到期ACT执行/拒绝的1,981组合核对独立公式。961种无拒绝组合均同步，
928种较早私有拒绝组合为单方就绪；报告绑定源码和完整矩阵摘要，保留双方反例。
环境结果为合成值，不测试原生合法性，不证明拒绝可达或不可达，不关闭一般门。
11项专项1.55秒、联合45项3.00秒通过；首次测试命令路径错误证据保留。
G0/G1、34/43、训练准入及冻结部署保持原状态。

## 2026-10-23 有战斗的预约快照续演限定验收

combat-snapshot-v1/EVIDENCE.md与evidence-summary.json记录双独立新进程结果。
两个进程各推进1110tick；tick110/600/1200六个根均含双方pending预约，各两条
60tick战斗分支逐tick完全相同，完整根、全局字节及预约历史恢复一致，句柄释放。
每进程双方ACT14/13次、正常取消5次；含分支的实体1469帧、公开HP损失1441帧。
21份归档的集合/内容SHA/源码/父构建/配置/清理独立核验通过；探针分别122.793/
127.903秒，所属引擎及29783端口清理通过。原始帧、mask、hex和引擎私有保留。

55项专项10.64秒通过，包含空WAIT拒收、双分支篡改及重算外层回执仍拒收；
与随机诊断和原快照联合240项通过20.67秒、零跳过。fixture测试不冒充原生结果。
全局MT主轨迹只读未见变化，不证明固定卡组消费者不可达；其它随机状态、一般
pending/fallback信念路径及完整搜索终局仍待验，G0/G1与34/43保持开放。
Full128快照1792736248：121组/242根/30976模拟，tick4392继续，未终局；
开局采样801/1024，terminal选优待验。冻结部署未改，G2–G8及五机放量未启动。
发布复核：2131项源码清单通过，公开回归573通过/16项缺私有库跳过（72.25秒），
主工程211通过（36.70秒），凭据扫描零发现；测试集合不相加。

## 2026-10-23 独立全局MT快照修复限定验收

global-rng-snapshot-v1/EVIDENCE.md和公开摘要记录双新进程8边界全部2504字节
恢复、重复restore、两条各10步WAIT续演摘要一致及进程/端口清理。19份原生
归档独立内容验收；原始内存和引擎保持私有，缺私有归档时不宣称完整可重验。
引擎SHA为fbd97f8f…7141e，原旧快照遗漏诊断保持历史含义。

绑定库SHA、进程、base、manager、generation，拒绝多slot/render与损坏快照，
反序列化前检查资格；新scope仅覆盖native payload和此全局MT，不覆盖全进程。
新47项与既有随机诊断联合185项通过、11.37秒。固定速猪实际使用、有战斗
续演和其它随机状态仍待证，status=verified_limited_scope，不关闭G0/G1。
Full128提交119组/238根/30464模拟至tick4322，tick4342继续；开局545/1024、
terminal选优运行，冻结部署未改。34/43登记不变，G2–G8及五机放量未启动。
发布复核：2118项源清单通过，公开回归518通过/16跳过（62.13秒），主工程
211通过（36.07秒），凭据扫描零发现；缺私有库的静态重验跳过，集合不相加。

## 2026-10-23 全局随机状态原生快照覆盖诊断

global-rng-audit-v1/EVIDENCE.md与evidence-summary.json记录双进程8个边界诊断。
INIT_ARRAY加载入口与三段消费者代码已绑定；2504字节全局MT对象的首字节
受控扰动在8次快照恢复后均保留，state-digest均不变，证实已测对象未被恢复/
摘要覆盖。扰动期间零推进，CAS修复原字节、句柄和所属进程清理通过，18份归档
独立验收。原始内存hex留在私有归档，公开只保存实现及SHA摘要。

WAIT轨迹中全局状态各自保持启动值；两个进程启动状态不同，未证明具体时钟值
或实际战斗影响。固定速猪消费者实际可达性仍待证，不升级为整体非确定性结论。
新增33项，与alias/shuffle/seed联合138项通过、6.06秒；公开缺私有库时静态
重算检查跳过。G0/G1及34/43保持开放；Full128、开局采样、terminal选优继续。
发布复核：2106项公开源清单通过，公开回归471通过/16项缺私有库跳过、39.82秒；
主工程211通过、23.46秒，凭据扫描零发现。不同范围的测试集合不相加。

## 2026-10-23 随机初始化跳转入口补查

独立rng-alias-audit-v1核对18处直接引用及19条跳转闭包边，定位time(NULL)作为
seed初始化全局对象0x19fc368的额外路径。绑定三段完整字节、PLT及导入符号；
不将EH范围当真实入口，不宣称路径已执行、固定速猪使用或快照覆盖。
这是一项明确待验项，非实际搜索受墙钟影响的证明。冻结部署保持原样。

本机新增31项，与洗牌/种子联合105项通过、5.37秒；对应真实库重算及五类篡改
检查在缺私有库的公开副本中跳过。Full128快照1792731888：117组/234根/29952
有效模拟，tick4302玩家1继续搜索、未终局。开局采样177/1024，terminal选优
运行，原生round2待验；G0/G1与34/43保持开放，不启动G2或五机放量。
发布复核：2094项源清单通过，公开回归439通过/15项缺私有库跳过、38.08秒；
主工程211通过、22.14秒；凭据扫描零发现。各测试集合重叠，不累加。

## 2026-10-23 恢复重试验收与独立开局诊断

active-migration-v1/retry-completion-evidence-20261023.json绑定失败父、成功retry、
外部launch/回执/验收SHA及真实选优交接。重试第5→7步完成，exit0、9430.322秒；
双归档内容验收通过，网络/AdamW/RNG/游标/选优状态一致，冻结部署复核通过。
native_game_provenance_reverified=false，原始对局来源未由此次验收重验；
不作G2、完整迁移或训练准入。父仍保留超时失败。真实terminal选优已在.35 GPU1
启动，独立28800秒，仍运行中；原生round2待验。

修复SSH派发控制器的公开目录边界误判，源CR和公开导出布局/.git文件覆盖共
32项通过0.158秒。未修改运行中冻结包。独立prior-sampling-v1预注册1024seed、
第二新进程64seed复测与21个Bonferroni校正检验；34项工程/篡改检查通过1.325秒，
原生采集进行中，无分布结论。首attempt远端缺SciPy在引擎启动前失败并保留；
统计改为本地惰性导入，新attempt独立，不修改seed及检验。

Full128快照1792731064已提交116组/232根/29696有效模拟至tick4266；
tick4294玩家1搜索中，无失败、未终局。G0/G1、34/43保持开放，不五机放量。
中心205.514GiB为瞬时可用量，两代数据容量未验。原始私有归档和权重独立保留。
发布复核：2088项公开源码清单通过；公开回归414通过、9项因缺私有库跳过，
37.95秒；主工程211通过、21.97秒；凭据扫描零发现。测试集合重叠，不相加。
公开测试依赖补入SciPy，统计仍只在本机运行，采集进程不新增依赖。

## 2026-10-23 手牌临时随机路径静态审计

`fair-final-native-shuffle-audit-v1/EVIDENCE.md`及`static-report.json`记录固定库的
14段字节摘要、18处MT初始化直接引用/唯一EH函数归属、手牌seed来源及同一栈对象
的generate/clear。正常返回控制流225指令/状态组合均经clear；分析排除异常和
非局部退出，不作全局调用图证明。ct1548/subtype55通过两级表分派及vtable另行定位。

本机新专项24项与旧seed50项联合74项通过，3.79秒、零跳过；缺私有库的公开版本
跳过对应静态重算/篡改检查，不把跳过当作原生通过。16处其它引用、间接分派、
实际路径观测、排列分布与先验校准仍待验。G0/G1及34/43登记保持开放。
Full128后台提交114组/228根/29184有效模拟至tick4170，未终局；active重试仍运行。
G2–G8、正式训练及五机放量未启动，运行中的冻结实现未变。
发布复核：2075项公开源清单通过；公开回归348通过/9项缺私有库跳过，38.62秒；
主工程211通过，23.99秒；凭据扫描零发现。各集合不累加。

## 2026-10-23 原生种子生命周期限定验证

`fair-final-native-seed-audit-v1/EVIDENCE.md`及`evidence-summary.json`绑定独立双进程
原始归档与验收SHA。合法WAIT轨迹各四阶段的只读检查、过期身份拒收、快照重复
续演、同进程重复reset及跨进程同seed状态一致；所属引擎清理完成，17份归档核验。
静态核对四段函数、构造器注册及time导入；本机50项工程测试通过1.68秒。

首个探针复用atomic连接执行90 tick warmup失败，原证据保留且验收拒收；新attempt
只在探针更换连接，未改冻结runtime。首次reset实际距base约35秒，缓存此前已非零，
两进程seed增量均0；未验证15秒内首次reset、原生增量1及全部隐藏随机机制。
完整搜索与恢复终局、一般原生边界仍待验，不新增整体门或34/43登记PASS。

Full128继续至112组/224根/28672有效模拟、tick4106；active恢复retry仍运行，
terminal选优未启动。G2–G8、正式训练及五机放量未启动。
公开回归331项通过、2项缺私有库跳过，57.77秒；主工程211项通过，36.01秒。
2067项公开源清单通过，凭据扫描零发现；测试范围重叠，不累加。

## 2026-10-23 超时恢复重试与显式选优交接

`fair-final-active-migration-v1` 新增独立恢复启动器、运行时与双归档验收器。
只接受四阶段 `[0,0,0,124]` 的超时父，287份输入683466574字节原样继承，
只恢复解冻第5→7步，父继续保留失败。原生retry已在.35 GPU1运行，独立28800秒；
尚待完成与独立内容验收，未启动terminal选优。

选优交接显式绑定失败父和成功retry，重核父失败记录/父子launch及回执/验收器SHA，
保留完整checkpoint和冻结部署；新状态与原四阶段成功状态分别处理。
工程测试含真实子进程及篡改拒收，但小模型夹具继续被真实日程门拒收。
测试与证据身份见 `active-retry-evidence-20261023.json` 和对应XML；失败报告保留。
联合工程测试141项通过，182.68秒；公开回归282通过/1跳过，57.91秒；
主工程211项通过，36.11秒。2056项公开源清单通过，凭据扫描零发现。
测试范围重叠，不累加；不代表原生完整恢复、terminal选优或整体门已通过。

Full128继续：107组/214根/27392有效模拟至tick4038，无失败、未终局。
随机状态覆盖未闭合；整体G0/G1及34/43保持，G2–G8、正式训练和五机放量未启动。

## 2026-10-23 组合provider原生local验收及执行参数审计

`fair-final-general-fallback-v1/native-local-evidence-summary.json`：四根512有效模拟、
tick110/142两组真实隐藏状态配对、未完成边零回传与引擎清理独立通过。
核验343项源码及33份归档。首锚点10.953秒，随后0.135/1.069/0.221秒；
只有四根，不外推恒定成本。原生pending/fallback、总体先验校准和完整终局仍待验。

`fair-final-native-execution-flags-v1/static-report.json`：固定库静态控制流核对
保存的参数2/3仅有两个显式bit1测试。15项本机分析器/篡改测试通过；对opaque
helper、整个执行器和两条队列不作等价证明，不解释未证实的错误码或宣称拒绝不可达。

本轮公开回归282项通过（35.800秒），缺私有库的单项跳过；主工程211项通过
（20.43秒）；2045项公开源清单通过，凭据扫描零发现。测试集合分别记录、不累加。
后台Full128继续：快照1792718966已提交105组，tick4036搜索中、无失败报告。
整体G0/G1与34/43登记保持，G2–G8未启动。以下为此前记录。

## 2026-10-23 原生队列mode限定验收

fair-final-native-mode-audit-v1独立绑定固定libg的27条predicate指令，
原生合法WAIT轨迹在tick90/1200/3600/5400均读得world_type=3、
predicate_bit0=1，对应bit0-set分支；不是旧queue审计中的bit0-clear/age20路径。
过期身份拒收、只读digest不变、快照重复续演和所属引擎清理独立通过。
32项工程/篡改测试通过；不证明所有实战分支、mask等价或合法pending可达。
下一项应针对bit0-set的ct86调用条件取证，不用有限轨迹声称全局不可达。
中心/home持久盘最新只读可用206.931198GiB，越过200GiB瞬时底线，
两代正式容量与正式中心链路仍待验；未清历史数据或调整原门槛。

## 2026-10-23 一般provider原生local路径验收启动

Full128后台继续；另用独立29771/29772双引擎部署general-fallback组合provider，
复用冻结history-v2真实轨迹，注册tick110与后继根、原始与隐藏牌序扰动配对，
每根128有效模拟。新增独立验收器校验外层组合及底层独立构造身份、抽牌链、
访问数、边界回传、恢复和清理；工程fixture不作为原生通过证据。
attempt为native-general-local-1792716408200694000，首根128模拟完成561.9998秒，
隐藏配对根计算中，四根结果待验。54项工程/验收测试通过。
此探针只验证local分支，原生pending/fallback、总体先验及G0/G1整体仍开放。

## 2026-10-23 Full128同局24小时续跑

旧段在tick3922完整边界保存并清理。用其SHA固定归档恢复244项源码，显式
加载冻结搜索协议，103组/206根/26368有效模拟和递归父链核验通过。新段
native-search-segment-1792715657103653000在.35启动，1 actor、2专属引擎。
用户指定86400秒上限，1800秒完整边界保存余量；历史21600秒记录继续可验。
启动器、worker、探针、收取及终局验收保持相同时限与源码身份语义。
51项专项通过9.16秒，见fair-final-search-continuation-v1/tests-24hour-20261023.xml；
工程测试不代替完整原生终局。新进程已重演211条日志至tick3922并开始新根，
恢复报告passed_replay_only；完整终局及最终独立验收仍待完成。
active父benchmark/reference/frozen成功，trainable总时限耗尽exit124，跨进程
恢复整体验收尚未通过；整体门与正式中心200GiB门保持，未启动正式训练。

## 2026-10-23 远程选优派发/回收原型

最新主线为`5c54e0478747d30be2472f855c06a871d8866406`。新增
`selection_remote_runtime.py` 与 `selection_ssh.py`：绑定目标GPU UUID及资源预检、
GPU独占lease、拒绝未知SSH主机密钥、原子SFTP上传，要求远端上传树与配置清单完全
一致；回收时交叉校验远端配置/进程/失败元数据的SHA、字节数和JSON内容，下载产物
后仍须通过既有独立选优验收器。

新专项29个测试案例通过；与handoff、execution和active verifier联合93项通过98.774秒，
重叠测试不累加，详见`fair-final-active-migration-v1/remote-selection-evidence-20261023.json`
及`tests-selection-remote-joint.xml`。SSH及远端文件系统均为模拟：没有真实主机连接、
GPU启动、父恢复或terminal选优；因此不新增G0/G1门PASS，不代表正式G2/G3或模型强度。
运行准入字段保持false，冻结部署保持原样。

核对日期：2026-10-23。本文是公开快照说明；完整历史见工程进度与冻结验收清单。

## 2026-10-22 既有成果与此前基线

最新基线为`8f68763803207890351f574ab7191a5a613cb505`，fetch全部远端分支、
本地HEAD/origin/main逐项核对一致，更新前2010项公开源SHA通过。新增独立预算
选优执行工具和终止内容验收：只运行交接中的冻结代码，保留完整第7步状态，
超时/中断清理独立进程组，失败隔离；校验terminal实际checkpoint、父历史、
latest/best游标及RNG和完整best继承。修复第12轮额外洗牌和Tensor字典比较。
联合64项通过184.31秒，包含新26项、此前17项及21项，不累加。真实子进程
执行使用显式fixture，小模型验证EARLY_STOP和MAX_EPOCHS，不作为原生PASS。
真实交接、原生terminal选优及SSH派发/回收仍待完成；证据见
`_spec_work/versions/fair-final-active-migration-v1/selection-execution-evidence-20261022.json`。

本轮发布复验：冻结参考检查220项通过（80.95秒），主工程CPU检查211项
通过（49.75秒），2015项公开源清单校验通过，gitleaks未发现凭据。
上述测试各自记录，不与64项专项测试累加，也不替代原生验收。

以下为此前成果记录。

本轮在最新主线`f9e0958e8fe671eaa22e42319dcfc599e89c2a89`上新增独立选优
交接准备工具。强制新版原生352/256/96日程和六checkpoint恢复验收，原字节
保留冻结包及第7步完整模型/AdamW/RNG/游标/选优历史，拒收失败父、fixture门和
旧batch链路；只准备、不执行。专项17项通过50.62秒，联合38项通过109.81秒，
包含前者，不累加。真实原生交接尚未运行。证据见
`_spec_work/versions/fair-final-active-migration-v1/selection-handoff-evidence-20261022.json`。
本轮冻结参考220项通过78.06秒、主工程211项通过51.41秒，分别为CPU工程检查。

上一轮fetch远端确认主线`da8ca6c12653add5cc4a8667f4809db9625a0ac7`，
清单2000项源SHA与工作区一致。新增active独立归档验收器，外部固定launch/
回执，核验部署、数据、四阶段独立进程和六个checkpoint的真实恢复状态，默认
强制352行、batch256、microbatch1、尾批96。专项21项通过60.40秒，工具/恢复/
验收联合31项通过98.27秒，范围重叠不累加；fixture不作为原生通过。
证据见fair-final-active-migration-v1/verifier-evidence-20261022.json。原生reference
已完成256行和96行两次更新，两个恢复阶段及最终独立验收仍待完成，整体门状态不变。

新增最新主线独立原生验证工具，见`fair-final-active-migration-v1/EVIDENCE.md`。
277文件载荷绑定六个active源码、独立scalar参考与新实现恢复身份；10项CPU
测试最近重跑通过32.96秒。首次CUDA解冻阶段AdamW更新超容差，失败归档287文件
保留：近零梯度差异被优化器放大。已修复教师/学生解冻时均走逐位置路径，冻结仍
合批；learning 11项、migration 26项通过。修复后GPU1六对CUDA损失/梯度/AdamW
比较通过，解冻三对最大梯度差0，快照1792676859已进入reference。冻结/解冻
跨进程恢复及最终独立归档核验待完成，不新增整体门PASS。主工程211项通过36.39秒，
冻结参考220项通过50.25秒，分开记录不累加。单帧配对加速比中位数1.01174，
冻结两个可ACT帧约6.14/6.54倍、解冻约1倍；不代表完整训练吞吐。

| 成果 | 实现与证据 | 证据边界 |
|---|---|---|
| FAIR 信息帧、预约时钟、自回归合法动作、完成边界 MCTS、回放和恢复 | firstlight_CR/native_runner/training/fair_final 及 tests/test_fair_final_* | 局部、内存及原生专项分别记录，整体门开放 |
| 增量信念与局部重条件化 | _spec_work/versions/fair-final-history-v2 | 冻结源码身份可核对；原生附件外置 |
| 局部路径不支持时的一般历史退化 | fair-final-general-fallback-v1/EVIDENCE.md | 73 项相关测试；内存双方 pending 四根 512 次模拟；不证明原生合法可达 |
| 原生合法预约专项 | fair-final-legal-reservations-v1 | 已有 57 分支、114 次重复续演，未观测拒绝；有限覆盖 |
| 迁移数据、消费、选优与断点恢复工具 | fair-final-migration-{series,selection,integration,cache}-v1 | 三局 1,044 行、FP32 batch256跨进程三次更新恢复独立通过；terminal选优、原生round2与正式迁移仍待验 |
| 迁移 KL 合批与教师条件化缓存 | fair-final-migration-batch-v1/EVIDENCE.md | 六对真实 CUDA 损失/梯度/AdamW对照通过；解冻encoder保留原反向图，完整五阶段链路仍待验 |
| 完整搜索局分段续跑与前缀验收 | fair-final-search-continuation-v1/CONTRACT.md | 同一局前缀保存与恢复，不是完整终局 PASS |
| 原生命令与队列静态审计 | fair-final-native-{command,queue}-audit-v1 | 联合 42 项通过；静态 CFG 不证明运行时分支或合法掩码等价 |
| 延迟补牌离线诊断修正 | fair-final-delayed-prior-audit-v1 | 三局 1,044 帧零排除、原生轨迹牌区对照；不证明总体先验校准 |
| 嵌套历史评测种子审计 | fair-final-migration-integration-v1/NESTED_SEED_EVIDENCE.md | 四组3,200条记录新增1,600个排除seed；联合190项通过，5条结局不明确记录不作WDL标签，原始附件外置 |
| 合批父包到采集包的确定性身份交接 | fair-final-migration-integration-v1/collection-runtime-lineage-evidence-20261022.json | 24项通过；实际276源码SHA复核、273父文件保持；仅本地准备，原生round2仍待验 |
| 后续采集预检与有界引擎worker | fair-final-migration-integration-v1/bounded-collection-verification-20261022.json | 43项联合测试通过；278源码采集包、273父文件保持；夹具不替代原生采集与独立整局验收 |
| 后续采集归档独立验收与多轮父身份 | fair-final-migration-integration-v1/selected-collection-verifier-evidence-20261022.json | 47项联合测试通过；重验终止选优父、整局与原始终局、种子来源和所属清理；仅归档内容及fixture范围 |

以上版本目录均位于 `_spec_work/versions/`。测试集合存在重叠，表格数字不相加。

## 2026-10-22 主工程修复分支（已整合、CPU验证通过）

- 已通过fast-forward整合分支 `audit/ci-kl-puct-inheritance-20261022` 最新提交 `8195a5a07b8bf7d9533fc7a3bfbe83b0c5083a99`，保留本机未发布成果，8个新增或修改源码同步回工作区。为当前 `firstlight_CR` 新增独立单测入口/CI；原冻结历史版本公开检查保留。
- 原生迁移批量KL在主工程加入独立批量Delay条件计算与校验。**源独立版本 `fair-final-migration-batch-v1` 的真实CUDA数值与原生恢复证据仍是权威参考**，不可用本分支单测取代；编码器解冻时保留逐位置学生反向图与逐位置KL加法顺序。
- PUCT新增先验首访、旧字典序首访基线；根噪声显式区分 `training/evaluation/ablation_no_noise`，协议SHA随配置变化。
- 权重继承新增基于逐Tensor一致性的覆盖率报告，当前能被证明直接继承的是语义Encoder，新Policy/WDL Head继续蒸馏，不凭相同形状虚报。
- 本机当前主工程211项通过24.12秒，冻结参考版220项通过39.00秒，分别属于CPU工程测试。所拉取提交的两套GitHub CI亦通过：[当前工程](https://github.com/Coder-Osman/Nashura/actions/runs/39779254288)、[冻结参考](https://github.com/Coder-Osman/Nashura/actions/runs/39779254266)。新实现CUDA与batch256原生恢复仍待验，不增加G0/G1 PASS，不启动G2或正式训练。交接与回执见 `agent-handoff/`。
- 归档验收器、五阶段归档核验及旧batch-v1在最新active源码下的兼容性检查75项通过69.40秒，3项因缺少私有冻结部署附件跳过。此项与上面的测试集合存在重叠，不累加；详细身份见 `agent-handoff/INTEGRATION_2026-10-22.json`。

## 当前运行任务的最后已知快照

快照时间戳1792681423/1792681789，以及此前独立恢复验收：

- Full128 搜索局旧段已提交86组后归档；新段恢复后已提交96组、192根、24,576次有效模拟至tick3644，tick3672玩家0搜索中，尚未原生终局。该任务继续使用原冻结部署。
- 确定性父训练探针已结束，执行1次FP32更新；尚无该更新后的验证选优结论。
- 严格确定性恢复reference与resumed均正常结束，完成归档及独立内容/执行链验收；模型、AdamW、RNG、采样游标/下一批及选优历史严格一致。证据见migration-cache-v1/native-deterministic-resume-verified-20261022.json；仅三局工程范围，不证明终止选优或正式G2。
- 旧迁移KL链路benchmark/training/reference/resumed均exit0；selection耗尽14400.468秒总预算后退出124，无terminal报告，最后持久化第7步/3个epoch。归档仍在下载，不作为合格选优父；六对单帧加速比中位数1.376，不代表整轮吞吐。
- 最新active链路的benchmark六对通过，reference已完成4次冻结更新和2个epoch，进程与部署匹配。冻结/解冻跨进程恢复未完成，旧实现结果不转记为active通过。
- .35 中心盘约137GiB，正式中心200GiB门保持；备用 .39 仅完成工程样本传输核验。

这些数字只表示当时进度。当前阶段不报告 G0/G1 闭合、正式训练启动或搜索提高实力。

## 历史实验

保留了 `_spec_work/evidence/` 中价值训练、教师采样、学生消费、连续训练及教师评测的 README。它们属于旧协议背景，部分引用的私有原始附件未公开；不代替新版 G2/G3 验收。

## 后续验收顺序

G0/G1 剩余正确性与完整局 → G2 正式迁移及 WDL 校准 → G3 搜索增益 → 后续训练和模型发布门。每阶段证据完成后再更新本页。
