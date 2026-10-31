# 阶段成果与证据索引

## 2026-10-31：组件组合的有界持续运行与内存趋势

固定VM、同一native/DEX与四输入，4进程严格串行完成132096局、128000测量局。
主机复算每进程早/中/晚延迟及RSS/PSS；晚/早耗时变化为+0.05%/-0.15%/-0.22%/+0.37%，
记录60–152KiB的RSS首末增长，不作无泄漏或跨会话加速结论。计时前后各120000组
ARM64对照和运行后8完整局22698帧通过。候选默认关闭，无并发、不扩服务，整体目标
继续。详见[报告](../agent-handoff/SERIAL_ARM64_COMPONENT_SUSTAINED_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-component-sustained-evidence-20261031.json)。

## 2026-10-31：组件目标 guard 独立确认方向一致

沿用同一observer和部署工具、固定VM/引擎/输入，反向case顺序重新完成12完整局34047帧、
计时前后各120000组ARM64对照与32批/6912局。主机309文件身份及独立复算通过，确认轮
四组中位耗时下降3.59% / 4.25% / 3.44% / 3.39%，八block及四组采样p95/p99均改善。
两轮共64批、12800测量局，方向一致；这是固定四输入的局部可重复收益，继续默认关闭。
下一优先级为同进程持续运行、动作边界和热点，不测并发、不扩外围服务，整体目标继续。
[报告](../agent-handoff/SERIAL_ARM64_COMPONENT_GUARD_CONFIRMATION_2026-10-31.md)与[机器摘要](../agent-handoff/serial-arm64-component-guard-confirmation-evidence-20261031.json)。

## 2026-10-31：组件检查目标 guard 的首轮串行增益

默认关闭的 component-guard-v1 / hot-distance-component-v1 保留原 bounds，只在 flag
bit0=0 且对象为空或当前虚函数目标精确匹配已 guard 的纯常量 getter 时省略冗余工作；
其余回退原实现。计时前后各120000组ARM64对照、12完整局34047帧与已观测状态通过。
主机核验309文件并独立复算32批/6912局，四组中位耗时下降3.28% / 4.03% / 3.20% / 3.03%，
八block均下降，采样p95/p99均改善。维持默认关闭，待不变构建的独立确认；不扩张服务、
不测并发，固定VM和最小依赖边界不变。详见
[报告](../agent-handoff/SERIAL_ARM64_COMPONENT_GUARD_2026-10-31.md)与[机器摘要](../agent-handoff/serial-arm64-component-guard-evidence-20261031.json)。

## 2026-10-31：查询 pair-load 独立复测仍有 block 回退

同一 observer、固定 VM/输入，反转四组计时顺序，重新完成 12 个完整局 34047 帧、
192424 组 ARM64 指令及 32 批 ABBA/BAAB。主机核验 308 个文件并独立复算，四组
中位耗时下降 1.56% / 0.85% / 0.20% / 0.90%，但 b1、b7 第一 block 分别回退
0.57% / 0.06%；b1 p99 略升。保持默认关闭，不把小幅增量视为完全稳定，下一优先级
回到已采样的对象/组件路径；固定最小依赖与串行边界不变。详见
[报告](../agent-handoff/SERIAL_ARM64_QUERY_DIMENSION_CONFIRMATION_2026-10-31.md)与[机器摘要](../agent-handoff/serial-arm64-query-dimension-confirmation-evidence-20261031.json)。

## 2026-10-31：查询维度 pair-load 的首轮限定收益

默认关闭的 query-dimension-pair-v1 / hot-distance-query-v1 仅合并查询内相邻宽高
读取，完整字节和入口 guard，无缓存、不改过滤。192424 组 ARM64 指令对照、
12 个完整局共 34047 帧与已观测状态通过；32 批 ABBA/BAAB、6400 测量局经
主机复算，四组中位耗时下降 1.92% / 1.46% / 0.68% / 1.71%。b7 有一批慢基线，
其 +4.86% 聚合吞吐不作稳定收益；a1 p99 略升，保持默认关闭并待独立复测。
固定 VM、最小依赖、串行边界不变，整体目标继续。详见
[报告](../agent-handoff/SERIAL_ARM64_QUERY_DIMENSION_PAIR_2026-10-31.md)与[机器摘要](../agent-handoff/serial-arm64-query-dimension-pair-evidence-20261031.json)。

## 2026-10-31：网格清理循环展开的限定结果

原始指令/候选 24107 组、四组输入三路径共 12 个完整局 34047 帧及已输出状态通过。
同一新 observer 的 hot-distance-v1 与加清理展开组合独立 ABBA/BAAB 32 批、
6912 局 / 6400 测量局经主机复算；耗时变化 -1.02% / +0.43% / -0.50% / -0.13%，
吞吐变化 +1.00% / -0.67% / +0.71% / +0.01%。a7 两 block 回退，b7 方向不一，
未证实一致增益，继续默认关闭。没有新增硬件 PMU 或长期稳定性结论。详见
[报告](../agent-handoff/SERIAL_ARM64_GRID_CLEAR_UNROLL_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-grid-clear-unroll-evidence-20261031.json)。

## 2026-10-31：组合路径的剩余 warm-step 热点

相同 hot-distance-v1 构建、四个固定输入独立软件 CPU-clock 采样，排除初始化、
warmup、reset、终态验证；后 200 局各获得 5414 / 5806 / 2638 / 2494 个窗口样本，
零丢样。空间查询约占 18.66%–20.17%，对象更新 6.37%–9.01%、组件更新
6.82%–8.65%；仅为 Mainloop/libg 叶样本占全部窗口样本，不能解释为函数耗时、
cycles、cache miss 或新的速度收益。精确 ELF 展开索引和 FDE 确定热点范围，
区分替换后代码与原始函数字节身份。完整四组原始归档和参考快照经主机独立
复算；pure_linux 1401 passed / 1 skipped。下一优先级仍为空间候选过滤与对象/
组件路径，没有新增外围服务。详见[报告](../agent-handoff/SERIAL_ARM64_COMBINED_PROFILE_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-combined-profile-evidence-20261031.json)。

## 2026-10-31：组合候选独立复测与串行稳定性

工程检查结束后才启动 guest，本轮计时期间没有再启动工程测试；该边界不等于
主机频率、热状态或其他负载完全隔离。相同 native observer、DEX 与固定输入
完成八个新完整局，全部 22698 帧和已观测状态一致。32 批 ABBA/BAAB 共 6912 局
全部通过，6400 测量局；组合相对原始四组耗时变化 -7.68% / -7.19% / -6.32% /
-6.35%，吞吐变化 +8.73% / +8.29% / +6.82% / +7.26%，八个 block 均下降。
每路径每组四个独立进程批次，不以批内重复局作 IID 或宣称总体显著性。

四组各一个组合进程，24 task × 516 局，共 49536 局正常终局退出。各进程
实际观察 351.483 / 374.451 / 171.525 / 163.965 秒，Mainloop TID 固定且跨批时钟
不重叠；RSS 首尾变化 +72 / +4 / 0 / -4 KiB。有限 RSS 和终态校验不证明
长期无泄漏、未序列化状态或每局全帧等价。主机独立复算 7295462 字节原始
归档及全部摘要；pure_linux 1392 passed / 1 skipped、公开 1104 passed / 42 skipped、
主工程 211 passed。候选仍默认关闭，最小依赖和固定 VM 保持不变，整体目标继续
进行。详见[报告](../agent-handoff/SERIAL_ARM64_COMBINED_FOLLOWUP_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-combined-followup-evidence-20261031.json)。

## 2026-10-31：getter 与饱和距离的组合增量

默认关闭的 hot-distance-v1 仅显式组合旧 11 点 getter 与两个距离函数，保持
固定构建 guard 和最小服务边界。四组两卡组/seed 1、7 的四路径共 16 完整局，
全部 serializer 帧与已观测状态一致。abdc/bcad/cdba/dacb 平衡顺序串行 64 批
共 7424 局正常退出，主机从 13021075 字节原始归档逐帧复核并独立复算。
组合相对原始四组耗时变化 -6.59% / -7.86% / -7.03% / -7.29%，吞吐提高
7.46% / 8.27% / 7.40% / 7.71%；相对 getter 单项再降 1.52%–2.55%，相对
距离单项再降 3.62%–5.47%。每组四 block 中组合均快于原始和两单项。
每路径每组仅四批，主机工程检查与部分计时重叠，不外推总体显著性、隔离负载、
未序列化状态或长期稳定性。纯 Linux 1370 passed / 1 skipped、公开 1104 passed /
42 skipped、主工程 211 passed；整体目标继续进行。详见
[报告](../agent-handoff/SERIAL_ARM64_HOT_DISTANCE_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-hot-distance-evidence-20261031.json)。

## 2026-10-31：饱和平方距离候选实测

真实采样定位两种整数距离函数，新候选保留 32 位差值回绕、INT32_MAX 饱和
和指针第二坐标的条件性读取。2234256 组 ARM64 原始指令/候选/参考结果一致；
四组新原始与候选共八个完整局的全部 serializer 帧及已观测状态与历史一致。
同 observer、固定 2 vCPU / 6144 MiB、ABBA/BAAB 串行 32 批共 6912 局正常
退出，剔除 warmup 后 6400 局。四组批次中位耗时变化依次 -3.49%、-3.96%、
-0.91%、-1.90%，聚合吞吐 +3.37%、+3.51%、+0.86%、+1.35%；各组两个
交错 block 都下降，每路径仅四批，未宣称总体显著性或全输入等价。
候选默认关闭，未与旧 getter 叠加；本阶段没有扩张 Android 服务或改变 VM。
纯 Linux 1330 passed / 1 skipped、公开 1104 passed / 42 skipped、主工程 211 passed。
整体目标继续进行，详见[报告](../agent-handoff/SERIAL_ARM64_SATURATED_DISTANCE_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-saturated-distance-evidence-20261031.json)。

## 2026-10-31：线程资源与有界同进程串行稳定性

四组资源诊断各测 200 局，Mainloop CPU / 包围墙钟为 99.970%–99.987%，主动
切换、major fault、块 I/O 均为零；不据此推断所有线程无等待。当前 guest 无硬件
PMU，cycles/instructions/cache 输出 not supported，退出 0 不计作取得计数。
八个原始/旧 11 点优化进程共 80 批、41280 局全部正常退出；每进程实际观察
71.247–169.511 秒，RSS 首尾未增长，不外推长期无泄漏。新 DEX 下八个完整局
全部 serializer 帧及已观测状态与历史一致；稳定性局仅验证终态，不冒充全帧门。
主机独立复算全部原始记录，保留初始解析失败。纯 Linux 1323 passed / 1 skipped、
公开 1104 passed / 42 skipped、主工程 211 passed。不测并发、不扩张服务、不改 VM
配置；原始后优化顺序不作新性能收益结论，整体目标继续进行。详见
[报告](../agent-handoff/SERIAL_ARM64_THREAD_RESOURCES_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-thread-resources-evidence-20261031.json)。

## 2026-10-31：对象分配诊断与组件 getter 三路径对照

9 窗口 / 11 调用点独立诊断显示，四组临时 new[]/delete[] 仅占对象窗口
0.71%–0.94%，组件更新占 73.66%–76.94%；含时钟及包装器开销，不作无插桩
性能比例。未引入缓冲池，保持最小 headless 依赖边界，未重建外围服务。

新增 41 个精确 getter BL→LDR，总 52 点，包含旧 11 点。四组原始/旧/新候选
加对象诊断共 16 个完整局，全部 serializer 帧及已观测状态与历史原始路径一致。
同 observer、固定 2 vCPU / 6144 MiB 下按六种全排列串行测量 72 批、8352 局，
剔除 warmup 后 7200 测量局；新相对旧候选批次中位耗时变化依次为
-0.57%、+0.75%、+0.45%、-1.01%，配对 block 4/2/2/5 个更快，没有稳定增量收益。
两候选继续默认关闭。主机独立重算全部原始摘要、全帧、观测和终态字节身份。
本机为 Fusion ARM64 Linux + ART/Bionic，无 QEMU 指令翻译，未宣称 macOS 原生。
详见[报告](../agent-handoff/SERIAL_ARM64_COMPONENT_GETTERS_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-component-getters-evidence-20261031.json)。

## 2026-10-31：动作 dispatch 与状态更新诊断

四组固定非空动作各运行原始、cmd 诊断、inject 诊断，共 12 局正常终局。
主机独立复算与历史原始局的全部 serializer 帧和已观测状态，四组均一致。
实际 mode 3 dispatch 50/49/22/19 次；mode 2 未命中，零返回不当作动作成功。
对象集合窗口占 state inclusive 的 92.94%–94.03%，queue 为 1.94%–2.97%；
均为含时钟/包装器开销的单次诊断，非 benchmark 或收益。完整计数支持下一步沿
对象/组件/分配路径优化，不增加外围 Android 服务。安装失败及时间戳量化失败
原样保留；整体目标继续进行。详见[报告](../agent-handoff/SERIAL_ARM64_NATIVE_PHASES_2026-10-31.md)
与[机器摘要](../agent-handoff/serial-arm64-native-phases-evidence-20261031.json)。

## 2026-10-31：热 getter 内联的串行完整局对照

固定 ARM64 build 的 11 个热 getter BL 调用点替换为原有 LDR；不缓存、不提前
读取、不重排，三调用者入口、全部 BL 和 getter LDR/RET 精确 guard。只改一次性
进程，原始库文件不变；候选默认关闭，与空间候选互斥。

两卡组、seed 1/7 的四组新原始局/优化局与历史原始局全部 serializer 帧和已观测
状态一致。相同 observer、输入和 VM 配置下按 ABBA/BAAB 运行 32 批、3712 局，
剔除每批 16 局 warmup 后共 3200 测量局。四组批次中位耗时变化为
`-5.89% / -5.52% / -5.22% / -5.00%`，聚合吞吐变化为
`+5.92% / +6.05% / +6.46% / +5.77%`。两个交错 block 分别都观察到耗时下降；
每 variant/case 只有四个独立批次，保留虚拟机调度、主机频率和热状态未锁定的边界。

pure_linux `1249 passed / 1 skipped`、公开 `1104 passed / 42 skipped`、
main_engine `211 passed`。没有并发、外围 Android 服务重建或系统配置变更；
当前 Mac → Fusion ARM64 Ubuntu 路径没有 QEMU 翻译层，但仍依赖 ART/JNI，
不宣称 macOS 原生/无 ART 运行。整体目标与长期稳定性继续未完成。详见
[报告](../agent-handoff/SERIAL_ARM64_HOT_GETTERS_2026-10-31.md)和
[机器摘要](../agent-handoff/serial-arm64-hot-getters-evidence-20261031.json)。

## 2026-10-31：多卡组完整局与查询候选实测

两个实际不同的八卡集合、seed 1/7 共四组固定非空动作，由真实 selection builder
生成并冻结。旧 observer 重复局、新 observer 默认局和 opt-in 优化局的全部 serializer
帧及已观测状态一致；终局分别 tick 3601、3601、2154、1989。范围仍限已覆盖 fixture
的序列化状态及观测字段，不包含未序列化全局或所有卡牌玩法。

空间查询候选只将超过 grid height 后的内层循环跳转到原始外层增量；默认关闭。
同构建 16 批 A/B/B/A 共 1,856 局（1,600 测量局）全部正常退出，中位均值变化
-0.20%、-0.19%、-0.95%、-1.17%；首组总吞吐略降，稳定因果收益未证实。
计时没有快照、详细观察或 profiler，VM 固定 2 vCPU / 6144 MiB。工程检查全部通过：
pure_linux 1242 / 1 skip，公开 1104 / 42 skip，主引擎 211。详见
[报告](../agent-handoff/SERIAL_ARM64_SPATIAL_Y_BOUND_2026-10-31.md)和
[机器摘要](../agent-handoff/serial-arm64-spatial-y-bound-evidence-20261031.json)。

## 2026-10-31：warm-step 窗口归因与串行 task 等待

新增离线 perf 汇总器，要求正常退出、完整终局/serializer 摘要、最小观测计划、精确
libg、零丢样和明确的 monotonic clockid，仅归因 measured step-loop 窗口。修复自有
runner 让 benchmark task 等待跟随外层 timeout 并保持原生 120 秒上限；216 局 profile
全部正常完成，剔除 1,291 个非 step 与 448 个 warmup 样本后保留 5,689 个 warm 样本。
libg 叶样本 5,629（98.95%）；空间候选查询范围叶样本 949（16.68%）。在线 CPU 样本
不代表 off-CPU 等待、函数耗时或硬件 PMU 指标。

同一构建的独立两批无 profiler 基线各 16 局预热、200 局测量，中位 29.428 / 29.023 ms，
吞吐 121,324 / 122,783 tick/s，step p50/p95/p99 分别 5.626/18.461/25.629 与
5.542/18.189/24.710 μs（各局抽样分位数的中位数）。终态均 tick 3601、摘要
`6c959326dd2f09cc`、3,091 字节；这不是全 payload 等价门。没有修改 native 计算、
没有优化前后收益结论，也未测并发。旧 CLI 失败、缺少 clock 绑定及长批次 `-5`/124
失败分别留证。详见[报告](../agent-handoff/SERIAL_ARM64_WARM_WINDOW_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-warm-window-evidence-20261031.json)。

## 2026-10-31：串行 step 采样相位复核

benchmark 对重复局的每 16 tick 定时采样增加确定性相位轮转，并在汇总时校验
phase period、相位顺序和每局样本数。ARM64 Ubuntu guest 使用相同的 seed 1、52 条
命令、八卡组与固定引擎，单进程运行 16 局预热加 30 局测量；46 局全部到 tick 3601，
终态 serializer 均为 `6c959326dd2f09cc` / 3091 字节。测量中位每局 68.67 ms、
吞吐 52,404 tick/s；step p50/p95/p99 为 12.81/43.96/62.77 μs，6752 个 sampled
step。没有运行并发 workload，也没有改动引擎。

与 10 月 30 日记录的 28.088 ms 中位值明显不同；旧数据来自不同 observer/计时路径，
两批新 46 局执行自身也出现中位波动。当前数据独立登记，不当作前后对照或优化收益；
CPU/虚拟机调度差异尚未归因。整体目标、跨卡组复验和优化收益仍未完成。详见
[阶段报告](../agent-handoff/SERIAL_ARM64_SAMPLE_PHASE_2026-10-31.md)与
[机器摘要](../agent-handoff/serial-arm64-sample-phase-evidence-20261031.json)。
Python 3.13.5 下纯 Linux `1189 passed、1 skipped`，公开检查 `1104 passed、42 skipped`，
主引擎 `211 passed`。系统 Python 3.9.6 首次运行因缺少 `cryptography` 有 16 项环境失败；
使用已安装该依赖的 Python 3.13.5 完整重跑通过。

## 2026-10-30：温热串行 step 基线与 seed 复验

ARM64 Ubuntu guest 中使用固定 52 命令和同一八卡组，单进程按序运行；没有并发测量。
seed 1 的独立 30 局基线每局中位 28.088 ms，step p50/p95/p99 为
5.500/21.835/72.858 微秒，6,780 个分层样本全部来自每 16 tick 采样。独立 resource
批次为每局中位 27.930 ms，计时边界外另记子进程 CPU、缺页和上下文切换；两批次
不是配对性能对照，不据其差值推断加速。

保持卡组和 52 条命令不变，只改 replay 顶层 `rndSeed`，seed 7/19/41 各重复 30 局。
三组均在各自真实终局结束，tick/digest/serializer 字节数分别为
2846/`530ed75967cbc6e0`/3207、3214/`aac146c6e46ea067`/3406、
3601/`5b4f9d94dbb4c59f`/2828；每组重复结果一致。该覆盖只有一个八卡组，
不是多卡组验收。perf 采样受 guest sudo 交互认证限制，本轮没有取得新调用栈 profile，
也没有应用引擎优化；READY 仍为 `UNVERIFIED`。

详见[报告](../agent-handoff/SERIAL_WARM_STEP_PROFILE_2026-10-30.md)与
[机器摘要](../agent-handoff/serial-warm-step-profile-evidence-20261030.json)。

## 2026-10-30：空间候选查询调用者静态审计

固定 profile 对应的 `libg.so` 静态反汇编确认调用点传入坐标与范围，目标按二维定点
网格枚举候选并执行状态/虚调用过滤。通用包装器有4处直接调用，所在上层函数有3处
直接调用；碰撞、寻敌或其他玩法语义仍未知。本轮没有运行 guest/replay、并发或性能
测量，也没有改动引擎二进制。详见[审计报告](../agent-handoff/SERIAL_ARM64_QUERY_CALLER_AUDIT_2026-10-30.md)
与[机器摘要](../agent-handoff/serial-arm64-query-caller-audit-evidence-20261030.json)。

## 2026-10-30：ARM64 headless 串行热点归属

固定 replay 的 `cpu-clock:u` profile 捕获 2,569 个样本且零丢样；
`libg.so+0xf34b3c..0xf35064` 范围命中 294 个样本。AArch64 指令表明它遍历二维
定点坐标格并筛选候选对象，推断为战斗执行路径中的空间候选查询；具体玩法语义
仍未知。profile 来自单 guest 串行运行，没有运行并发，也未实施优化。
离线复核保留的 script/maps 得到相同 294 个命中，且全部来自 `Mainloop`；该复核未重跑
workload。原始 guest `perf.data` 读取被文件权限拒绝，较早 v2 报告仍未完成输入身份绑定。
同 fixture 独立基线为每局中位 28.280 ms、p95 30.306 ms，不与采样 profile
做速度比较。多 seed、跨卡组和整体验收仍未完成。详见[报告](../agent-handoff/SERIAL_ARM64_CPU_PROFILE_2026-10-30.md)
与[机器摘要](../agent-handoff/serial-arm64-cpu-profile-evidence-20261030.json)。Python 3.13.5
下纯 Linux `1183 passed、1 skipped`、公开检查 `1104 passed、42 skipped`、主引擎
`211 passed`；快照校验 2,878 项通过，gitleaks 无发现。

## 2026-10-30：Headless replay 路径诊断

原始 replay 和把相同 52 条命令转为显式注入的路径均到 tick 3601 原生终局，
摘要相同（`6c959326dd2f09cc`，3091 字节）。两条路径均消费 50 条队列命令，
最终留 2 条；历史 v38 的 730 条 observation/player/object 记录逐行一致。
阶段计时是每条路径单次样本，不作为性能比较；本轮未测并发，未生成 snapshot
归档。整体目标仍未完成。详见[报告](../agent-handoff/HEADLESS_REPLAY_ROUTE_DIAGNOSTICS_2026-10-30.md)
与[机器摘要](../agent-handoff/headless-replay-route-diagnostics-evidence-20261030.json)。

## 2026-10-30：替代ARM64转译器

FEX转译方向相反，未测；arm64emu-user JIT与上游QEMU11.1.3均完整真实
终局并通过3602帧归档比对。固定同机两核及observer，旧QEMU30局中位
2168.240ms，新上游30局3106.611ms，独立JIT20局4857.950ms。
当前没有更快替换，继续旧QEMU；整体ready保持未验证。最终纯Linux
1176 passed、1 skipped。详见[报告](../agent-handoff/ALTERNATIVE_TRANSLATORS_2026-10-30.md)
和[机器摘要](../agent-handoff/alternative-translator-evidence-20261030.json)。

## 2026-10-30：x86服务器Android与ARM Linux转译

同机两核、各30局顺序实测全部原生终局，独立预检3602帧逐字节相同。
ARM Linux + QEMU step中位2145.899ms，1675.878 tick/s；
Android + native bridge为2471.171ms，1452.439 tick/s。
进程RSS约354.50 / 357.96MiB，不与整个emulator内存混排。
完整批次初始化/收尾和ART配置不同，冷启动及并发密度尚未测量。
同源探针显示转译成本随计算类型变化；真实跨机47~54倍尚未精确拆分。
最终源码ARM guest v56identity五阶段对象关系和原生终局归档通过，
不替代完整ResourceManager初始化语义。纯Linux1175 passed、1 skipped，
公开9组1104 passed、42 skipped，主工程211 passed。详见
[报告](../agent-handoff/X86_SERVER_HEADLESS_COMPARISON_2026-10-30.md)与
[证据](../agent-handoff/x86-server-headless-comparison-evidence-20261030.json)。

## 2026-10-30：零Binder完整对局复验

直接ART入口保留原始VM/native注册，省去app_process的Binder启动流程。v52零Binder
设备open/ioctl下tick3601原生终局、退出0，3602帧与Android字节一致。仓库runner的
v53/v54复验同样通过，其中v54屏蔽oldroot/sys。新入口性能尚未重测，原始wrapper、
ART传递闭包和ResourceManager身份边界仍保留。详见[复验说明](../tools/pure_linux/HEADLESS_RUNTIME.md)。

## 2026-10-30：最小 headless 真实首局与固定 replay

真实 ARM64 Ubuntu 中，原始 libg 以八卡组完成部署、扣费、移动、伤害与原生终局
（tick3601，finalized=1）。52条固定命令原始loadReplay再次得到相同终局，
661条采样字段一致。显式serializer适配在tick900保存、恢复并续演到终局，
两次同进程GameState重建的终态摘要一致；原始wrapper空controller失败保留。
原始客户端wrapper等价性和完整目标仍待验证。
详见[报告](../agent-handoff/PURE_LINUX_FIRST_MATCH_2026-10-30.md)与
[机器证据](../agent-handoff/pure-linux-first-match-evidence-20261030.json)。

同版本Android API30 ARM64 headless基线已完成配对：3602个逐tick原始serializer
payload字节完全一致。各30局单进程与1/2/4进程700局密度测量均通过终态检查；
详见报告中的CPU配置差异和测量范围，完整目标继续进行。

独立namespace入口已在缩减/dev与空/sys根视图下完成同一完整replay及3602帧字节
配对；Binder启动前置与oldroot内SELinux残留可见性见报告，未宣称依赖已完全最小化。

## 2026-10-30：最小 headless 原生生命周期

v17 在真实 ARM64 Ubuntu/Bionic/ART 中，以原始 APK DEX 和精确 libg，执行原始
native create/start/resume/pause/stop/destroy 并自然退出0。使用 APK 局部元数据、
真实 AssetManager/Resources 和 flags=0 的本地 Intent；本轮未启动 system_server、
Display、SurfaceFlinger、GPU/EGL 或原始 init。holder/game 对象非空、虚表匹配已知
Android 对象，Mainloop 位于事件循环；销毁后 owner/filesystem 清空、Mainloop 消失。
内容 root 仍为空；状态 `HEADLESS_NATIVE_LIFECYCLE_OBSERVED_LIMITED`，严格 catalog
及 ResourceManager、GameState、reset/step、动作、快照、完整对局、基线对照和性能
保持未验证。v12/v13 将带标签指针读取失败当作空对象的结论撤回；v14 显式报错，
v15 修正读取偏移后取得真实对象。历史日志保持原样。
详见[报告](../agent-handoff/PURE_LINUX_HEADLESS_LIFECYCLE_2026-10-30.md)和
[机器摘要](../agent-handoff/pure-linux-headless-lifecycle-evidence-20261030.json)。

## 2026-10-30：Display statsd linker 依赖复验

在同一 ARM64 Ubuntu guest 的已加载 permissive policy 和隔离 namespace 中，
r5 输入将同一 statsd APEX 的 `libstatssocket.so`、`libstatspull.so` 只读 bind
到 `/system/lib64`，并为 SurfaceFlinger、GPUService、allocator 和 composer 固定
输入身份。两库均被 SurfaceFlinger/GPUService 的真实 linker 路径从
`/system/lib64` 打开，旧的 statsd 库可见性缺口已越过，状态为
`DISPLAY_STATS_LINKER_VISIBILITY_PASS_LIMITED`。

同一 run 的三次 property 写入与同镜像 `getprop` 读回 `1/2/3` 全部通过，693 个
输入身份未改变，`kernel/kptr_restrict`、`vm/mmap_rnd_bits` 和
`vm/mmap_rnd_compat_bits` 均恢复；policy 保持 permissive/已加载，按实验约定需
guest 重启后撤回。SurfaceFlinger 随后因 guest 缺少 `/dev/goldfish_pipe`、OpenGL
host pipe 和 EGL 初始化条件而以127结束，composer 因 framebuffer 设备缺失以1结束；
allocator 以234结束，GPUService 在清理前保持运行。`system_services_ready=false`，
Display Binder 探测、严格 `CATALOG_READY_PASS`、GameApp Context、ResourceManager、
GameState、Step、完整对局和性能仍未通过或未运行。

私有主机归档为 111,462,400 字节，SHA-256 为
`0f9020ed9a8d48b0985c421e1c57e6e0677af0f7b277301ce45ee6e5bd631674`；guest 中本轮
输出与临时归档已在复制后清理。详见[报告](../agent-handoff/PURE_LINUX_DISPLAY_STATS_LINKER_2026-10-30.md)、
[证据](../agent-handoff/pure-linux-display-stats-linker-evidence-20261030.json)。

## 2026-10-30：Display 服务依赖边界

v25 的原始 init trial 在 ARM64 guest 中成功完成 APEX bootstrap，5 个 bootstrap APEX
均挂载，三次 property 写入/同镜像读回通过，输入身份、loop cleanup 和三项 sysctl
恢复通过。Display probe 已进入 ART、System Context、Power/Thermal、ServiceRegistry
fetcher 与 `PowerManagerInternal` 前置链；随后因 `/dev/binder` 没有 `SurfaceFlinger`
而等待并由 9 秒上限收尾。状态为 `DISPLAY_SERVICE_ENTRY_REACHED_LIMITED`，
`system_services_ready=false`。GameApp Context、严格 `CATALOG_READY_PASS`、
ResourceManager、GameState、Step、完整对局、Android 对照和性能保持未通过或未运行。
详见[报告](../agent-handoff/PURE_LINUX_DISPLAY_SERVICE_BOUNDARY_2026-10-30.md)、
[证据](../agent-handoff/pure-linux-display-service-boundary-evidence-20261030.json)。

## 2026-10-30：ART 服务观察器 guest 限定运行

扩展原始 ART probe 的 Thermal/Power Binder、ServiceRegistry fetcher、Context 缓存槽
及 PowerManagerInternal 观察；guest probe 的 13 项 Power/Thermal/fetcher 观测全部
存在，进程退出 0。三次 property 写入与同镜像读回通过，输入身份不变，三个 sysctl
恢复，policy 仍为 permissive/已加载。该结果为 `ART_SERVICE_FETCHER_TRACE_RUNTIME_OBSERVED_LIMITED`，
`system_services_ready=false`；Display/SurfaceFlinger、package 服务、GameApp Context、
严格 catalog 与完整游戏链路仍未通过或未运行。纯 Linux 1,138 项、公开检查 1,104 项、
主引擎 211 项通过，公开检查有 42 项跳过。详见[报告](../agent-handoff/PURE_LINUX_ART_SERVICE_FETCHER_TRACE_2026-10-30.md)
和[证据](../agent-handoff/pure-linux-art-service-fetcher-trace-evidence-20261030.json)。

## 2026-10-29：原始 APEX 激活与持续属性服务

原始 apexd 已校验并实际挂载五个 bootstrap APEX，原始 BoringSSL 64 位自检返回
0；三次 property 读回、最终自动 loop 清理和 sysctl 恢复通过。v14 启动原始
hwservicemanager 与 suspend HAL，并观察到 `ISystemSuspend/default` 注册；HAL 受 guest
电源 sysfs 缺口限制，DisplayManagerService 仍因 PowerManager 对象为空而失败。
状态为 `ORIGINAL_APEX_BOOTSTRAP_PASS_LIMITED`，系统服务与完整游戏链路继续未验收。
纯 Linux 专项回归为 `1138 passed、1 skipped`，主引擎为 `211 passed`。详见[报告](../agent-handoff/PURE_LINUX_APEX_BOOTSTRAP_2026-10-29.md)和
[证据](../agent-handoff/pure-linux-apex-bootstrap-evidence-20261029.json)。以下记录保留历史边界。

## 2026-10-29：原始 init 属性写入与读回

真实 property v2 写入已由同镜像 getprop 独立读回，连续值 `1、2、3` 的限定验证
通过；真实 SELinux 域转换、linkerconfig 与 apexd bootstrap 均已返回，三项 sysctl
已恢复。服务启动期间仍因原始 APEX 激活闭包缺失而中断，PowerManager / DisplayManager、
GameApp Context 和完整游戏链路继续未验收。详见[报告](../agent-handoff/PURE_LINUX_INIT_PROPERTY_READBACK_2026-10-29.md)
和[证据](../agent-handoff/pure-linux-init-property-readback-evidence-20261029.json)。以下各节保留历史边界。

## 2026-10-29：android_servers 加载与系统服务依赖

修正 system → ART 的 `libart.so` 导出后，原始 `android_servers` 加载和系统
Context 均返回；子进程的真实优先级许可也已验证。原始 PowerManagerService
随后在 BatterySaverController 写系统缓存属性时退出，可写 property service
仍是共同依赖。状态为 `ANDROID_SERVERS_LOAD_PASS_LIMITED`，系统服务、GameApp
Context 及完整游戏链路继续未验收。详见[报告](../agent-handoff/PURE_LINUX_SYSTEM_SERVICE_DEPENDENCIES_2026-10-29.md)和
[证据](../agent-handoff/pure-linux-system-service-dependencies-evidence-20261029.json)。

## 2026-10-29：原始 init property service 请求边界

ARM64 Linux 中，原始 init 在三次隔离复测中均生成真实 property socket，并接收
`sys.system_server.start_count=1` 请求，返回权限拒绝 `24 / 0x18`；阶段状态为
`INIT_PROPERTY_REQUEST_REJECTED_LIMITED`。SELinux policy 尚未加载，init 后续在真实
kptr_restrict 写入处 fatal，退出 `6`。指定输入与观测状态前后相同，property
readback、系统服务及游戏完整链路继续未验收。详见[报告](../agent-handoff/PURE_LINUX_INIT_PROPERTY_SERVICE_2026-10-29.md)
和[证据](../agent-handoff/pure-linux-init-property-service-evidence-20261029.json)。

## 2026-10-29：真实系统服务启动边界

在真实 SELinuxfs、binderfs、同镜像 `servicemanager` 和原始 `services.jar` 下调用
`SystemServer.main()`；执行到 `SystemProperties.native_set` 时因隔离环境没有原始
property service 退出。另以同镜像 ADB pairing APEX 依赖尝试加载 `libandroid_servers.so`，
首轮缺库，补入原始库后在 linker 阶段 `SIGSEGV`。display/package 服务和 `GameApp`
Context 仍未启动，状态为 `SYSTEM_SERVICES_BLOCKED_LIMITED`。详见
[报告](../agent-handoff/PURE_LINUX_SYSTEM_SERVICES_BOUNDARY_2026-10-29.md)和
[证据](../agent-handoff/pure-linux-system-services-boundary-evidence-20261029.json)。

## 2026-10-29：真实 ART 下的 holder setup 生命周期

真实 ARM64 Linux guest 中，原始 APK DEX 与精确 `libg.so` 完成 holder setup 限定
生命周期：payload 偏移 `24`，allocator 前缀和分配块自指针匹配；holder 析构、
filesystem manager 替换/保留、provider release、`dlclose` 均返回，进程 exit `0`。
setup 后 data/update provider 路径被清空，`GameApp` Context 仍为空，故状态为
`NATIVE_VERIFIED_LIMITED`，不是完整初始化或 catalog ready。Activity/系统 Context
探测的框架缺口已保留。Android v4 与 Linux final-v1 的跨端 holder 身份审计 exit `0`，
验证 setup 事件关联、输出顺序和输入身份一致。详见[报告](../agent-handoff/PURE_LINUX_HOLDER_SETUP_REAL_ART_2026-10-29.md)、
[证据](../agent-handoff/pure-linux-holder-setup-real-art-evidence-20261029.json)。

在随后真实 SELinux/Binder 实验中，原始 `servicemanager` 通过 `BINDER_SET_CONTEXT_MGR_EXT`，
`ActivityThread.systemMain()` 返回真实 `android.app.ContextImpl`，包名为 `android` 且资源对象存在；
`IServiceManager` descriptor/ping 通过。display/package 服务尚未启动，`GameApp` Context 仍为空。
状态为 `SYSTEM_CONTEXT_PASS_LIMITED`，证据见
[pure-linux-system-context-evidence-20261029.json](../agent-handoff/pure-linux-system-context-evidence-20261029.json)。

## 2026-10-29：真实 ART、JNI 生命周期及 libg VM 绑定

Ubuntu ARM64 Linux 中已通过真实 Binder 协议、ART Java 计算和 JNI pthread
attach/callback/detach；精确引擎经 Java `System.load` 后，VM 槽与 ART 的真实
`JavaVM*` 相同，环境 getter 返回当前 `JNIEnv*`，mask 7、exit 0。运行时专项为
`PASS_LIMITED`，仍记录 15 个 Java 类缺失与 property SELinux 标签 `EPERM`。
本阶段未重跑 holder setup，严格 `CATALOG_READY_PASS=false`，ResourceManager、
GameState、Step、完整对局、Android 基线对照和性能未验收。详见
[报告](../agent-handoff/PURE_LINUX_ART_JAVAVM_2026-10-29.md)、
[复现步骤](../agent-handoff/PURE_LINUX_ART_REPRODUCING_2026-10-29.md)和
[机器证据](../agent-handoff/pure-linux-art-javavm-evidence-20261029.json)。

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
