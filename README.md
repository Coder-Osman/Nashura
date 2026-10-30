# Nashura

## 2026-10-31：组件检查目标 guard 的首轮串行增益

默认关闭的 component-guard-v1 / hot-distance-component-v1 保留原 bounds，只在 flag
bit0=0 且对象为空或当前虚函数目标精确匹配已 guard 的纯常量 getter 时省略冗余工作；
其余回退原实现。计时前后各120000组ARM64对照、12完整局34047帧与已观测状态通过。
主机核验309文件并独立复算32批/6912局，四组中位耗时下降3.28% / 4.03% / 3.20% / 3.03%，
八block均下降，采样p95/p99均改善。维持默认关闭，待不变构建的独立确认；不扩张服务、
不测并发，固定VM和最小依赖边界不变。详见
[报告](agent-handoff/SERIAL_ARM64_COMPONENT_GUARD_2026-10-31.md)与[机器摘要](agent-handoff/serial-arm64-component-guard-evidence-20261031.json)。

## 2026-10-31：查询 pair-load 独立复测仍有 block 回退

同一 observer、固定 VM/输入，反转四组计时顺序，重新完成 12 个完整局 34047 帧、
192424 组 ARM64 指令及 32 批 ABBA/BAAB。主机核验 308 个文件并独立复算，四组
中位耗时下降 1.56% / 0.85% / 0.20% / 0.90%，但 b1、b7 第一 block 分别回退
0.57% / 0.06%；b1 p99 略升。保持默认关闭，不把小幅增量视为完全稳定，下一优先级
回到已采样的对象/组件路径；固定最小依赖与串行边界不变。详见
[报告](agent-handoff/SERIAL_ARM64_QUERY_DIMENSION_CONFIRMATION_2026-10-31.md)与[机器摘要](agent-handoff/serial-arm64-query-dimension-confirmation-evidence-20261031.json)。

## 2026-10-31：查询维度 pair-load 的首轮限定收益

默认关闭的 query-dimension-pair-v1 / hot-distance-query-v1 仅合并查询内相邻宽高
读取，完整字节和入口 guard，无缓存、不改过滤。192424 组 ARM64 指令对照、
12 个完整局共 34047 帧与已观测状态通过；32 批 ABBA/BAAB、6400 测量局经
主机复算，四组中位耗时下降 1.92% / 1.46% / 0.68% / 1.71%。b7 有一批慢基线，
其 +4.86% 聚合吞吐不作稳定收益；a1 p99 略升，保持默认关闭并待独立复测。
固定 VM、最小依赖、串行边界不变，整体目标继续。详见
[报告](agent-handoff/SERIAL_ARM64_QUERY_DIMENSION_PAIR_2026-10-31.md)与[机器摘要](agent-handoff/serial-arm64-query-dimension-pair-evidence-20261031.json)。

## 2026-10-31：网格清理展开未获得一致增益

基于真实清理热点实现默认关闭的四项循环展开；ARM64 原始/候选指令 24107 组通过，
12 个完整局共 34047 帧及已观测状态一致。32 批 ABBA/BAAB、6400 测量局复算后，
相对 hot-distance-v1 耗时变化 -1.02% / +0.43% / -0.50% / -0.13%；a7 两 block
均回退、b7 方向相反，不升级默认。固定 VM、最小依赖，无并发。详见
[报告](agent-handoff/SERIAL_ARM64_GRID_CLEAR_UNROLL_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-grid-clear-unroll-evidence-20261031.json)。

## 2026-10-31：组合候选之后的四组 warm-step 热点

同一组合构建对四个固定输入独立 perf 采样并主机复算；仅保留后 200 局 warm-step
窗口，四组 5414 / 5806 / 2638 / 2494 个样本、零丢样。空间查询仍占约
18.66%–20.17%，对象/组件更新仍是下一优先级；精确 ELF 展开索引与 FDE 核对
函数范围，并区分替换后指令和原始 ELF 身份。此轮只作归因，没有新增速度收益，
不扩张 Android 服务或调整 VM。详见[报告](agent-handoff/SERIAL_ARM64_COMBINED_PROFILE_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-combined-profile-evidence-20261031.json)。

## 2026-10-31：组合候选独立复测与有界串行稳定性

本轮全部工程检查结束后才启动 guest，并保持计时期间无本轮工程测试。八个新
完整局的 22698 个 serializer 帧及已观测状态一致；32 批 ABBA/BAAB 共 6912 局、
6400 测量局主机独立复算，组合相对原始耗时下降 6.32%–7.68%，吞吐提高
6.82%–8.73%。另四进程各 24 个串行 task，共 49536 局通过，实际观察
163.97–374.45 秒/进程；RSS 首尾变化 -4 至 +72 KiB，不据此证明长期无泄漏。
候选默认关闭，固定 2 vCPU / 6144 MiB，不测并发、不扩张 Android 服务；
当前 Fusion ARM64 路线无 QEMU 指令翻译，整体目标继续进行。详见
[报告](agent-handoff/SERIAL_ARM64_COMBINED_FOLLOWUP_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-combined-followup-evidence-20261031.json)。

## 2026-10-31：旧 getter 与饱和距离的组合增量

新增默认关闭的 hot-distance-v1，显式组合旧 11 点 getter 与两个饱和距离函数。
四路径共 16 个完整局全部 serializer 帧及已观测状态与原始参考一致；64 批
平衡顺序串行测量、7424 局全部正常退出，主机解压逐帧并独立复算。组合相对
原始耗时下降 6.59%–7.86%、聚合吞吐提高 7.40%–8.27%；相对 getter 单项再降
1.52%–2.55%，相对距离单项再降 3.62%–5.47%，四组各四个 block 均快于两单项。
每路径每组仅四批，且部分计时与主机工程检查重叠，不作总体显著性或隔离负载
结论；保持固定 VM、最小依赖和串行边界。详见
[报告](agent-handoff/SERIAL_ARM64_HOT_DISTANCE_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-hot-distance-evidence-20261031.json)。

## 2026-10-31：饱和平方距离 native 候选

基于真实采样定位两种整数平方距离函数，保持 32 位差值回绕、饱和与条件性读取。
ARM64 guest 的 2234256 组原始指令/候选对照通过；四组共八个完整局全帧及已
观测状态一致。32 批 ABBA/BAAB、6912 局全部正常退出，批次中位耗时降低约
0.91%–3.96%，聚合吞吐提高约 0.86%–3.51%；主机独立复算。候选默认关闭，
不与旧 getter 收益相加；固定 VM 与最小依赖边界，整体目标继续进行。详见
[报告](agent-handoff/SERIAL_ARM64_SATURATED_DISTANCE_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-saturated-distance-evidence-20261031.json)。

## 2026-10-31：线程资源诊断与有界串行稳定性

四组 warm-loop 独立诊断中，Mainloop 线程 CPU / 包围墙钟约 99.970%–99.987%，
未观察到主动切换、major fault 或块 I/O；当前 guest 没有取得硬件 PMU 计数。
八个同进程串行运行共 80 批、41280 局，全部正常终局退出；新 DEX 的八个原始/
旧 11 点优化完整局全帧与已观测状态均一致。有限观察时长及 RSS 不代替长期无
泄漏证明，原始后优化顺序不作新加速结论。VM 配置与最小服务边界保持不变，
整体目标继续进行。详见[报告](agent-handoff/SERIAL_ARM64_THREAD_RESOURCES_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-thread-resources-evidence-20261031.json)。

## 2026-10-31：对象更新分解与组件 getter 三路径实测

新增对象临时分配与组件更新独立诊断；四组分配/释放仅占对象诊断窗口约
0.71%–0.94%，本轮不引入缓冲池。新 52 点候选与原始、旧 11 点候选共 16 个
完整局的全部 serializer 帧及已观测状态一致；72 批、8352 局串行测量完成。
新候选相对旧候选耗时变化 -0.57% / +0.75% / +0.45% / -1.01%，增量不一致，
保持默认关闭。当前 Mac 使用 Fusion ARM64 Linux guest，没有 QEMU 指令翻译；
仍依赖 ART/Bionic，不是 macOS 原生移植。详见
[报告](agent-handoff/SERIAL_ARM64_COMPONENT_GETTERS_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-component-getters-evidence-20261031.json)。

## 2026-10-31：动作 dispatch 与状态更新归因

新增默认关闭、与 benchmark 互斥的原始调用点诊断。四组固定输入共 12 个新完整局，
全部 serializer 帧及已观测状态与历史原始路径一致。八个诊断局中，对象集合窗口
占所测 state inclusive 约 92.94%–94.03%，queue 约 1.94%–2.97%；含插桩开销，
不作性能收益结论。确认 mode 3 dispatch 为 50/49/22/19 次，输入总数不代替 apply
次数。保留安装失败与时钟量化修正证据，后续优先对象/组件/分配路径。详见
[报告](agent-handoff/SERIAL_ARM64_NATIVE_PHASES_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-native-phases-evidence-20261031.json)。

## 2026-10-31：热 getter 候选的四组串行收益

同一 observer 在 11 个热调用点内联原有字段读取，默认关闭且不叠加空间候选。
两卡组、seed 1/7 的四组完整局全部 serializer 帧及已观测状态一致；32 批共
3712 局正常退出。交错 ABBA/BAAB 对照中，warm-loop 批次中位耗时降低约
5.00%–5.89%，聚合吞吐提高 5.77%–6.46%。这是当前 guest/fixture 的限定收益，
不代替长期稳定性、全部状态等价或整体目标；不测并发、不扩张 Android 服务。
本机使用 Fusion ARM64 Linux guest，当前链路没有 QEMU 指令翻译。详见
[报告](agent-handoff/SERIAL_ARM64_HOT_GETTERS_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-hot-getters-evidence-20261031.json)。

## 2026-10-31：多卡组完整局配对与空间查询候选

冻结两个不同八卡集合、seed 1/7 的原生非空动作；四组完整局的全部 serializer 帧及
已观测状态配对一致。首个 opt-in native 候选仅跳过查询的越界 y 尾部，默认关闭。
同构建 A/B/B/A 共 1,856 局正常完成，中位变化约 -0.19% 至 -1.17%，首组吞吐略降，
尚无稳定因果加速结论。整体性能目标继续进行，不测并发、不扩张 Android 服务。
详见[报告](agent-handoff/SERIAL_ARM64_SPATIAL_Y_BOUND_2026-10-31.md)与
[机器摘要](agent-handoff/serial-arm64-spatial-y-bound-evidence-20261031.json)。

## 2026-10-31：warm-step 窗口归因与长串行等待修复

修复 benchmark 重复局共享 Mainloop task 时的约五秒默认等待上限；216 局串行运行
现在正常返回。新汇总器以明确的 monotonic 时钟只保留后 200 局 step 窗口：5,689 个
CPU-clock 样本中，libg 叶样本占 98.95%，空间候选查询范围占 16.68%。另两批无 profiler
基线各测 200 局，中位 29.43 / 29.02 ms，吞吐 121,324 / 122,783 tick/s；两列为相同
构建重复测量，没有 native 优化收益结论。保留全部失败及单 seed/卡组、终态摘要等
覆盖边界，整体目标继续进行。详见[阶段报告](agent-handoff/SERIAL_ARM64_WARM_WINDOW_2026-10-31.md)
及[机器摘要](agent-handoff/serial-arm64-warm-window-evidence-20261031.json)。

## 2026-10-31：串行 step 采样相位复核

headless benchmark 现在在串行 reset 间轮转每 16 tick 的采样相位，并验证日志中的
phase、phase period 与样本数。ARM64 guest 完成 16 局预热、30 局测量；46 局均到
tick 3601，serializer 终态摘要一致。当前测量中位 68.67 ms、吞吐 52,404 tick/s，
step p50/p95/p99 为 12.81/43.96/62.77 μs。与 10 月 30 日旧记录差异明显，observer
与计时路径不同，不作前后性能结论；本轮没有改引擎。多卡组覆盖、优化收益和整体
readiness 仍未完成。纯 Linux `1189 passed、1 skipped`、公开检查 `1104 passed、42 skipped`、
主引擎 `211 passed`，公开源码清单 2,885 项和 gitleaks 均通过。详见
[阶段报告](agent-handoff/SERIAL_ARM64_SAMPLE_PHASE_2026-10-31.md)
和[机器证据](agent-handoff/serial-arm64-sample-phase-evidence-20261031.json)。

## 2026-10-30：空间查询调用者静态审计

继续反汇编已归属的 ARM64 热点：坐标和范围进入二维网格候选查询；同一包装器有四个
直接调用点，上层函数有三个调用点，具体玩法语义和回调身份仍未知。本轮只读静态分析，
未启动 replay、未测并发或性能，也未修改引擎二进制。详见[调用者审计](agent-handoff/SERIAL_ARM64_QUERY_CALLER_AUDIT_2026-10-30.md)
和[机器摘要](agent-handoff/serial-arm64-query-caller-audit-evidence-20261030.json)。
整体目标仍未完成。

## 2026-10-30：ARM64 headless 串行 profile 热点归属

单 fixture 固定 replay 的 `cpu-clock:u` profile 捕获 2,569 个样本、零丢样；
`libg.so+0xf34b3c..0xf35064` 范围命中 294 个样本。反汇编支持“二维空间格候选
查询”的行为推断，具体玩法名称仍未确认。该函数在 `Mainloop` 的固定 replay
调用路径中；没有做多 seed、并发测量或优化。详见[报告](agent-handoff/SERIAL_ARM64_CPU_PROFILE_2026-10-30.md)
和[证据](agent-handoff/serial-arm64-cpu-profile-evidence-20261030.json)。Python 3.13.5
下纯 Linux 1183 项、公开检查 1104 项和主引擎 211 项通过；整体目标仍未完成。

## 2026-10-30：Headless replay 路径诊断

原始 replay 与 `cmd` 注入 replay 在同一 ARM64 fixture 均到 tick 3601 原生终局，
终态摘要同为 `6c959326dd2f09cc` / 3091 字节；历史 v38 的 730 条
observation/player/object 记录逐行相同。单次解析/追加计时仅用于阶段诊断，
未测并发，也不构成性能基线。整体目标继续未完成。详见[报告](agent-handoff/HEADLESS_REPLAY_ROUTE_DIAGNOSTICS_2026-10-30.md)
和[机器摘要](agent-handoff/headless-replay-route-diagnostics-evidence-20261030.json)。

## 2026-10-30：替代ARM64转译器实测

相同Xeon两核、固定真实对局及相同observer，原QEMU8.2.2复测30局中位
2.168s，上游QEMU11.1.3为3.107s（30局），arm64emu-user JIT为4.858s
（20局）。候选完整逐tick归档通过，但本轮没有测到更快替换。FEX方向
为ARM64主机运行x86，与当前任务相反。保持最小headless服务边界，
整体ready仍未验证。详见[替代转译器报告](agent-handoff/ALTERNATIVE_TRANSLATORS_2026-10-30.md)。

## 2026-10-30：x86服务器转译效率实测

同一Xeon服务器、相同两核预算顺序测量各30局，ARM Linux + QEMU与
Android x86_64 + ARM native bridge均到tick3601，独立预检3602帧字节一致。
step循环中位2.146s / 2.471s，吞吐1676 / 1452 tick/s；前者高15.38%。
完整批次启动至退出约132s / 112s，不能从step优势外推端到端优势。
跨本机ARM原生执行的47~54倍混合了硬件和转译差异；新增同机计时/计算探针，
明确保留未精确归因及未测并发边界。最终ARM guest五阶段内容对象图身份
与完整replay归档复验通过，完整目标继续进行。详见
[服务器报告](agent-handoff/X86_SERVER_HEADLESS_COMPARISON_2026-10-30.md)和
[机器证据](agent-handoff/x86-server-headless-comparison-evidence-20261030.json)。

## 2026-10-30：最小 headless 原生生命周期

停止将完整系统/图形服务作为战斗默认依赖。真实 ARM64 Linux 中，以原始 APK DEX、
精确 libg、APK 局部 Context/Resources 和本地 Intent 执行原始 native
create/start/resume/pause/stop/destroy，进程自然退出0；holder/game 对象实际建立，
销毁后 Mainloop 消失。内容 root 仍为空，严格 catalog、ResourceManager、GameState、
reset/step/action/snapshot、完整对局与性能继续未验证。修正 ARM64 带标签指针的
观察器误读并保留全部失败历史。详见[报告](agent-handoff/PURE_LINUX_HEADLESS_LIFECYCLE_2026-10-30.md)
和[证据](agent-handoff/pure-linux-headless-lifecycle-evidence-20261030.json)。

## 2026-10-30：Display 服务依赖边界

v25 在用户目录输出、复用 service-v2 工具链后，原始 `apexd-bootstrap` 成功挂载 5 个
bootstrap APEX；三次 property 写入与同镜像 `getprop` 读回通过，loop cleanup、输入
身份和 sysctl 恢复通过。Display probe 已加载 `android_servers`、System Context 和
Power/Thermal/fetcher 前置链，随后在 `/dev/binder` 等待不存在的 `SurfaceFlinger`，
9 秒上限收尾。状态为 `DISPLAY_SERVICE_ENTRY_REACHED_LIMITED`，
`system_services_ready=false`；GameApp Context、严格 catalog、ResourceManager、
GameState、Step、完整对局、Android 对照和性能仍未验收。详见[报告](agent-handoff/PURE_LINUX_DISPLAY_SERVICE_BOUNDARY_2026-10-30.md)、
[证据](agent-handoff/pure-linux-display-service-boundary-evidence-20261030.json)和
[回执](agent-handoff/CODEX_REPLY_2026-10-30_DISPLAY_SERVICE.md)。

## 2026-10-30：ART 服务观察器 guest 限定运行

扩展原始 ART 服务探针，加入 Thermal/Power Binder、ServiceRegistry fetcher、Context
缓存槽及 PowerManagerInternal 观察；纯 Linux 1,138 项、公开检查 1,104 项、主引擎
211 项通过，公开检查有 42 项跳过。扩展后的
在 guest 中观察到原始 Power/Thermal Binder、ServiceRegistry fetcher、Context 缓存槽和
PowerManagerInternal；三次 property 写入与同镜像读回通过，输入身份不变且 sysctl
恢复。该结果为 `ART_SERVICE_FETCHER_TRACE_RUNTIME_OBSERVED_LIMITED`，
`system_services_ready=false`，Display/SurfaceFlinger、package 服务和完整游戏链路未验收。
详见[报告](agent-handoff/PURE_LINUX_ART_SERVICE_FETCHER_TRACE_2026-10-30.md)和
[证据](agent-handoff/pure-linux-art-service-fetcher-trace-evidence-20261030.json)。

## 2026-10-29：原始 APEX 激活与持续属性服务

原始 apexd 已校验并实际挂载五个 bootstrap APEX，原始 BoringSSL 64 位自检返回
0；三次 property 读回、最终自动 loop 清理和 sysctl 恢复通过。v14 已启动原始
hwservicemanager 与 suspend HAL，并观察到 `ISystemSuspend/default` 注册；HAL 受 guest
电源 sysfs 缺口限制，DisplayManagerService 仍因 PowerManager 对象为空而失败。
状态为 `ORIGINAL_APEX_BOOTSTRAP_PASS_LIMITED`，系统服务与完整游戏链路继续未验收。
纯 Linux 专项回归为 `1138 passed、1 skipped`，主引擎为 `211 passed`。详见[报告](agent-handoff/PURE_LINUX_APEX_BOOTSTRAP_2026-10-29.md)和
[证据](agent-handoff/pure-linux-apex-bootstrap-evidence-20261029.json)。以下记录保留历史边界。

## 2026-10-29：原始 init 属性写入与读回

真实 property v2 写入已由同镜像 getprop 独立读回，连续值 `1、2、3` 的限定验证
通过；真实 SELinux 域转换、linkerconfig 与 apexd bootstrap 均已返回，三项 sysctl
已恢复。服务启动期间仍因原始 APEX 激活闭包缺失而中断，PowerManager / DisplayManager、
GameApp Context 和完整游戏链路继续未验收。详见[报告](agent-handoff/PURE_LINUX_INIT_PROPERTY_READBACK_2026-10-29.md)
和[证据](agent-handoff/pure-linux-init-property-readback-evidence-20261029.json)。以下各节保留历史边界。

## 2026-10-29：android_servers 加载与系统服务依赖

修正 system → ART 的 `libart.so` 导出后，原始 `android_servers` 加载和系统
Context 均返回；子进程的真实优先级许可也已验证。原始 PowerManagerService
随后在 BatterySaverController 写系统缓存属性时退出，可写 property service
仍是共同依赖。状态为 `ANDROID_SERVERS_LOAD_PASS_LIMITED`，系统服务、GameApp
Context 及完整游戏链路继续未验收。详见[报告](agent-handoff/PURE_LINUX_SYSTEM_SERVICE_DEPENDENCIES_2026-10-29.md)和
[证据](agent-handoff/pure-linux-system-service-dependencies-evidence-20261029.json)。

## 2026-10-29：原始 init property service 请求边界

ARM64 Linux 中，原始 init 在三次隔离复测中均生成真实 property socket，并接收
`sys.system_server.start_count=1` 请求，返回权限拒绝 `24 / 0x18`；阶段状态为
`INIT_PROPERTY_REQUEST_REJECTED_LIMITED`。SELinux policy 尚未加载，init 后续在真实
kptr_restrict 写入处 fatal，退出 `6`。指定输入与观测状态前后相同，property
readback、系统服务及游戏完整链路继续未验收。详见[报告](agent-handoff/PURE_LINUX_INIT_PROPERTY_SERVICE_2026-10-29.md)
和[证据](agent-handoff/pure-linux-init-property-service-evidence-20261029.json)。

## 2026-10-29：真实系统服务启动边界

同一 ARM64 guest 中已实际调用同镜像 `SystemServer.main()` 与
`android_servers` 加载入口。`SystemServer` 在真实 property service 缺失处停止；
补入同镜像 ADB pairing 库后，`libandroid_servers.so` 又在不完整 linker 闭包处
触发 `SIGSEGV`。display/package 服务和 `GameApp` Context 仍未建立，阶段状态为
`SYSTEM_SERVICES_BLOCKED_LIMITED`。详见[阶段报告](agent-handoff/PURE_LINUX_SYSTEM_SERVICES_BOUNDARY_2026-10-29.md)
和[证据摘要](agent-handoff/pure-linux-system-services-boundary-evidence-20261029.json)。

## 2026-10-29：真实 ART 下的 holder setup 生命周期

在 Ubuntu ARM64 guest 的真实 binderfs/ART 闭包中，以原始 APK `classes.dex` 和精确
`libg.so` 完成 holder setup 的限定生命周期验证：payload 偏移 `24`、allocator
前缀、manager 替换/保留、holder 清理、provider release 和 `dlclose` 均返回，进程
exit `0`。`GameApp` Context 仍未初始化，setup 后 data/update 路径为空，严格
catalog、ResourceManager、GameState、Step、动作、快照、完整对局和性能门继续关闭。
Android v4 与 Linux final-v1 的跨端 holder 身份审计已 exit `0`，状态仍为
`NATIVE_VERIFIED_LIMITED`，provider 配置语义和 catalog 门保持关闭。详见[阶段报告](agent-handoff/PURE_LINUX_HOLDER_SETUP_REAL_ART_2026-10-29.md)和
[证据摘要](agent-handoff/pure-linux-holder-setup-real-art-evidence-20261029.json)。

随后在真实 SELinux/Binder 接口下启动同镜像 `servicemanager`，`ActivityThread.systemMain()`
返回真实 `android.app.ContextImpl`（包名 `android`，资源存在），Binder
`IServiceManager` descriptor/ping 通过。display/package 服务与 `GameApp` Context 仍未建立，
状态为 `SYSTEM_CONTEXT_PASS_LIMITED`。详见[系统 Context 证据](agent-handoff/pure-linux-system-context-evidence-20261029.json)。

## 2026-10-29：ARM64 Linux 真实 ART/JavaVM 与 libg VM 绑定

真实 Binder 协议、ART Java 计算、JNI 原生线程 attach/callback/detach 均通过；
精确身份 `libg.so` 经 Java `System.load` 返回，游戏 VM 槽与真实 `JavaVM*` 相同，
原生环境 getter 返回当前 `JNIEnv*`，exit 0。阶段为 `LOAD_AND_VM_BINDING_PASS_LIMITED`，
仍有 15 个游戏 Java 类缺失；holder setup 尚未在此新运行时重跑，严格 catalog、
ResourceManager、GameState、Step、完整对局和性能门继续未验收。详见
[报告](agent-handoff/PURE_LINUX_ART_JAVAVM_2026-10-29.md)、
[复现步骤](agent-handoff/PURE_LINUX_ART_REPRODUCING_2026-10-29.md)和
[证据](agent-handoff/pure-linux-art-javavm-evidence-20261029.json)。以下记录保留历史边界。

## 2026-10-29：Android ART 闭包启动边界

同构 Ubuntu ARM64 guest 中的临时 ART 闭包完成 `/proc` bind mount、`pivot_root` 和
linker 依赖解析；`app_process64` 已加载 `libandroid_runtime.so` 与 `libart.so`，
但 guest 没有 Binder 驱动，`JNI_CreateJavaVM` 在 `android::ProcessState` 打开
`/dev/binder` 时返回 `ENOENT`（占位普通文件对照为 `ENOTTY`），进程以 `SIGABRT`
（134）退出。阶段状态为 `BLOCKED_GUEST_BINDER`，JavaVM 指针没有返回；Linux holder setup、严格 catalog、ResourceManager、GameState、真实 Step、
完整对局和性能验收仍保持原有阻塞/未运行状态。详见
[阶段报告](agent-handoff/ANDROID_ART_CLOSURE_APP_PROCESS_BINDER_BLOCK_2026-10-29.md)
和[证据摘要](agent-handoff/android-art-closure-app-process-binder-evidence-20261029.json)。

## 2026-10-29：Linux holder setup 真实执行与 JavaVM 阻塞

恢复 guest 登录通道后，使用当前 probe 在 Ubuntu ARM64 中真实执行
`--holder-setup-only`：内容 root、filesystem 和 data/update provider 注册均返回，
holder setup 在分配器和首个对象分配后于 `libg.so+0x17d8b14` 对空 `JavaVM` 指针
触发 `SIGSEGV`。输入、资源和辅助文件前后身份一致；状态为
`RUNTIME_BLOCKED_JAVAVM`，严格 catalog、ResourceManager、GameState、真实 Step、
完整对局和性能仍未运行。详见[阶段报告](agent-handoff/PURE_LINUX_HOLDER_SETUP_JAVAVM_BLOCK_2026-10-29.md)
和[机器证据](agent-handoff/pure-linux-holder-setup-javavm-block-evidence-20261029.json)。

## 2026-10-29：worker2 后续工程复验

接续 holder setup 边界工作后，使用 Python 3.13.5 完成公开工程门复验：纯 Linux
专项 `1105 passed、1 skipped`，公开主组 `794 passed、27 skipped`，主引擎专项
`211 passed`，其余公开分组全部 exit 0；公开快照和 `gitleaks` 也通过。Ubuntu
ARM64 guest 的 SSH 22 端口仍拒绝，Linux holder setup 继续保持
`BLOCKED_ENVIRONMENT / NOT_RUN`，严格 catalog、ResourceManager、GameState、真实
Step、完整对局和性能验收仍未运行。详见[复验记录](agent-handoff/CODEX_REPLY_2026-10-29_REVALIDATION.md)。

## 2026-10-29：Linux 原生持有者设置阶段

Android holder setup 追踪稳定通过，设置槽从空变为非空并与后续初始化关联；Linux
侧已加入真实入口 guard、对象/分配器检查、清理契约和审计器。当前 Ubuntu ARM64
guest 的 `vmrun` 登录缺少有效密码且 SSH 端口拒绝，所以 Linux setup 保持
`BLOCKED_ENVIRONMENT`，没有生成 Linux 运行结果。纯 Linux 专项测试为
`1105 passed、1 skipped`；严格 catalog、ResourceManager、GameState、真实 Step、
完整对局和性能验收仍未运行。详见[报告](agent-handoff/PURE_LINUX_HOLDER_SETUP_2026-10-29.md)
和[机器证据](agent-handoff/pure-linux-holder-setup-evidence-20261029.json)。

## 2026-10-29：Linux 原生持有者工厂生命周期

在 ARM64 Linux 隔离子进程中，以真实 `libg.so` 验证参数工厂和持有者工厂的
创建、字段关系、析构及分配器释放；跨端身份审计通过，相关测试为1094 passed、
1 skipped。状态为 `NATIVE_VERIFIED_LIMITED`，严格 catalog 门仍为 `false`，配置
异常、Backup 等价、ResourceManager、GameState、Step、完整对局和性能验收未运行。
详见[报告](agent-handoff/PURE_LINUX_NATIVE_HOLDER_FACTORY_2026-10-29.md)和
[机器证据](agent-handoff/pure-linux-native-holder-factory-evidence-20261029.json)。

## 2026-10-29：完成函数查询键

双端候选查询的30字节键一致，Android返回非空、Linux返回空；查询容器及填充来源
待查。详见[报告](agent-handoff/PURE_LINUX_FINISH_LOOKUP_KEY_2026-10-29.md)。

## 2026-10-29：catalog 完成函数候选查询

两侧 context 槽非空；同一候选查询 Android 返回非空、Linux 返回空。查询输入及
初始化来源仍待核验。详见[报告](agent-handoff/PURE_LINUX_FINISH_LOOKUP_2026-10-29.md)。

## 2026-10-29：catalog 完成调用边界

第5批候选 `finish_a` 进入时17槽计数一致，返回时出现1,417行差异；内部原因
仍待验证。详见[报告](agent-handoff/PURE_LINUX_CATALOG_FINISH_PHASES_2026-10-29.md)。

## 2026-10-29：catalog 差异的批次边界

17个差异槽在前4批及第5批入口计数一致；第5批返回时首次出现1,417行差异。
详见[报告](agent-handoff/PURE_LINUX_CATALOG_BATCH_BOUNDARIES_2026-10-29.md)。

## 2026-10-29：对齐 update 后的 catalog 布局

两侧均使用相同 908 项 update 后，17 槽、1,457 行差异仍在；Linux 990→908
的已覆盖布局和行字段不变。详见[报告](agent-handoff/PURE_LINUX_MATCHED_UPDATE_LAYOUT_2026-10-29.md)。

## 2026-10-29：catalog 批次返回序列对照

修复 Android 回调异常后，两侧 5 个批次的 313 次 getter 返回序列一致；update
输入与表行内容仍有差异。详见[报告](agent-handoff/PURE_LINUX_CATALOG_ENTRY_COMPARISON_2026-10-29.md)。

## 2026-10-29：Linux catalog 批次内入口观测

Linux 真实引擎的 5 次批次及 313 次候选 getter 调用均记录返回；Android 附加超时，
跨平台对照和严格 catalog 门仍未通过。详见[报告](agent-handoff/PURE_LINUX_CATALOG_SCOPED_ENTRY_TRACE_2026-10-29.md)。

## 2026-10-29：catalog 逻辑资源对象类型

独立 Android/Linux 观察中597条逻辑路径的 APK、update 和缺失类别对应；Android
update 清单在采集期间变化，字节及解析等价尚未验证。详见[报告](agent-handoff/PURE_LINUX_CATALOG_RESOURCE_SELECTION_2026-10-29.md)。

## 2026-10-29：catalog 批量参数与资源路径

Android 和 Linux 真实进程各5次批量调用参数一致，已观察597个 `csv_logic/` 请求
路径集合一致；update 输入和解析内容仍未对齐。详见[报告](agent-handoff/PURE_LINUX_CATALOG_BATCH_TRACE_2026-10-29.md)。

## 2026-10-29：catalog 差异槽对象字段对照

17个差异槽的 Linux 2,078个字段值均包含于 Android 对应槽，Android 额外1,457个；
Android 清单门仍失败，字段语义未完全确认。详见[报告](agent-handoff/PURE_LINUX_CATALOG_ROW_FIELD_2026-10-29.md)。

## 2026-10-28：Android catalog 采集期间 update 清单变化

两次独立启动前后分别为990和908项，布局重复但严格清单门均拒收；删除原因未知。
详见[报告](agent-handoff/PURE_LINUX_CATALOG_UPDATE_DRIFT_2026-10-28.md)。

## 2026-10-28：Android 与 Linux catalog 行数差异

真实运行观察到162个相同表槽，但Android 25,810行、Linux 24,353行，17槽不同。
两次输入和阶段尚未完全对齐，根因未知；严格 catalog 门保持关闭。
详见[报告](agent-handoff/PURE_LINUX_CATALOG_LAYOUT_DIFFERENCE_2026-10-28.md)。

## 2026-10-28：历史 Android 与 Linux loader 的 update 资源身份

两边990个 update 文件路径及 SHA 一致，Linux loader 实际打开的131个路径全部匹配；
catalog 表值和 Backup 契约仍待验证。详见[报告](agent-handoff/PURE_LINUX_CATALOG_UPDATE_IDENTITY_2026-10-28.md)。

## 2026-10-28：历史 Android 与 Linux loader 的 APK 资源身份

两份不同整包 APK 的9,829个资源逐项 SHA 一致，Linux loader 的619个暂存资源
与 APK 清单一致；update 与 catalog 表值仍待对照。详见[报告](agent-handoff/PURE_LINUX_CATALOG_APK_IDENTITY_2026-10-28.md)。

## 2026-10-28：原生 Backup 队列中断恢复

ARM64 Linux/Bionic 中复现发布后进程中断，显式恢复保留请求并完成原生归档；
仅覆盖一个中断边界，Android Backup 等价及严格 catalog 仍未通过。
专项988通过/1跳过，详见[报告](agent-handoff/PURE_LINUX_BACKUP_CRASH_RECOVERY_2026-10-28.md)。

## 2026-10-28：Android E1 与 Linux 资源局部对照

历史 Android 归档与真实 ARM64 Linux libg 的24个案例在字节及文件操作上匹配；
独立身份校验通过，专项981通过/1跳过。严格 catalog 与 Backup 仍未通过。
详见[报告](agent-handoff/PURE_LINUX_E1_PAIRED_RESOURCE_2026-10-28.md)。

## 2026-10-28：Linux 原生资源路由矩阵

真实 ARM64 Linux libg 对13个合成 APK/update 冲突、缺失和回退案例按 E1 对应路由
规则运行；首轮因 probe 对无效文件调用 length 失败，修正后13/13及独立验收通过。
第三轮缺失例执行 provider release、dlclose 和 asset finish；Android 配对字节、完整 catalog 和 Backup 等价仍未通过。
专项973通过/1跳过，详见[报告](agent-handoff/PURE_LINUX_RESOURCE_MATRIX_2026-10-28.md)。

## 2026-10-28：真实 Linux catalog 资源轨迹

已有原版 libg loader 轨迹显示247次 APK open/read/close、131个 update 文件映射关闭；
122个 update 文件与 APK 同名，其中118个清单 SHA 不同。路径集合与 mmap 只提供有限
运行证据，不证明请求级因果或 Android 字节一致；严格 catalog 仍未通过。专项964通过/1跳过。
详见[报告](agent-handoff/PURE_LINUX_CATALOG_RESOURCE_TRACE_2026-10-28.md)。

## 2026-10-28：非空偏好编码键与真实 libg 复核

修正非空偏好表使用明文键统计的缺陷，按绑定材料逐槽计算编码键；缺少材料或加密库时保持未知。
真实 ARM64 Linux libg 对已有目标键的双槽运行 native exit 0，条目数均为 1，loader
观察到 ready 和162张表布局；严格 wrapper exit 1。专项957通过/1跳过；Backup等价、
完整资源语义和严格catalog仍未闭合。详见[报告](agent-handoff/PURE_LINUX_NONEMPTY_PREFERENCE_RECHECK_2026-10-28.md)。

## 2026-10-28：受控配置写入与原值恢复复核

隔离 API 30 ARM64 AVD 中，真实配置值从 `server-login` 写入测试值后恢复，并经只读
复核保持原值；双槽四次 commit 均成功，Backup 请求进入服务端数据变更与 job 调度。
目标包原本就在 30 项 pending 队列中，本窗口未观察 journal 写入，且未采集偏好文件
的写前/中间态字节，因此不宣称跨进程恢复、Backup 交付或 Linux 等价。全量专项
`953 passed, 1 skipped`，主工程 `211 passed`，公开回归 `1,104 passed, 42 skipped`；
严格 `CATALOG_READY_PASS` 仍 BLOCKED，GameState/Step 未运行。
详见[报告](agent-handoff/PURE_LINUX_CONTROLLED_STORE_RECHECK_2026-10-28.md)、
[证据](agent-handoff/pure-linux-controlled-store-recheck-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_CONTROLLED_STORE.md)。

## 2026-10-28：当前配置身份与单事务字节对照

当前Android材料与真实libg三进程身份已绑定；一次受控store后恢复原值，
两槽及进程身份不变。ARM64 Linux两个重放进程exit0；修复空map XML格式后，
写入/恢复共8项字节对照全部一致，原始差异批次保留。
专项951通过/1跳过，主工程211通过，公开1104通过/42跳过；Backup服务等价性与异常域仍未闭合，
严格CATALOG_READY_PASS保持BLOCKED，ResourceManager/GameState/Step仍NOT_RUN。
详见[报告](agent-handoff/PURE_LINUX_RUNTIME_PREFERENCE_BINDING_2026-10-28.md)、
[证据](agent-handoff/pure-linux-runtime-preference-binding-evidence-20261028.json)和
[回执](agent-handoff/CODEX_REPLY_2026-10-28_RUNTIME_PREFERENCE.md)。以下保留历史状态。

## 2026-10-28：原生 Backup 候选绑定与自然 loader

真实ARM64 libg默认对照退出96，两个显式文件系统候选子进程退出0；
各自然loader53步、ready 0→1、162张表/24,353行，并正常析构及关闭。
双槽各1086bytes经独立消费者验证归档后清pending；原始材料和资源身份不变。
专项900/1、主工程211、公开1104/42均exit0。Android Backup等价性与当前材料身份
仍未闭合，严格catalog及三个wrapper仍BLOCKED；GameState/Step均NOT_RUN。
详见[报告](agent-handoff/PURE_LINUX_NATIVE_BACKUP_BINDING_2026-10-28.md)、
[证据](agent-handoff/pure-linux-native-backup-binding-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_BINDING.md)。以下保留历史状态。

## 2026-10-28：真实 Backup 失败重排与重试

专用Android实例中，一次自有store/request后agent抛真实IOException并重排；
没有追加请求的第二次消费成功。73份命令归档审计通过，XML保持215bytes，
成功transport231bytes、state72bytes；日志流259bytes。v1缺失文件退出码缺陷如实保留，
v2修正使用远端状态，未重复真实写入。新增47项工具测试，隔离专项857/1、主工程211、
公开1104/42均exit0。专用5582已关闭；Linux Backup绑定与严格catalog仍BLOCKED。
详见[报告](agent-handoff/PURE_LINUX_BACKUP_RETRY_2026-10-28.md)、
[证据](agent-handoff/pure-linux-backup-retry-evidence-20261028.json)和
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_RETRY.md)。以下保留历史状态。

## 2026-10-28：非空 Backup 与真实队列消费

自有Android fixture一次非空备份成功后目标仍pending；随后一次队列消费清除目标。
transport233bytes、XML217bytes、state72bytes前后一致，四份存活journal不含目标。
新增37项测试；专项810通过/1跳过、主工程211通过、公开1104通过/42跳过。
专用5582已关闭，Linux Backup绑定及严格catalog仍BLOCKED；失败路径与header语义未闭合。
详见[报告](agent-handoff/PURE_LINUX_BACKUP_PAYLOAD_2026-10-28.md)、
[证据](agent-handoff/pure-linux-backup-payload-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_PAYLOAD.md)。以下保留历史状态。

## 2026-10-28：真实 Backup JobService 回调与交付边界

专用ARM64实例中，自有应用一次请求后受控提前执行真实job，观察到
onStartJob→backupNow→cancel原方法返回；10事件、零drop/error/active。
工作线程仅日志覆盖且本次0bytes，非空transport、journal删除及失败路径仍未知。
首轮观察器崩溃证据保留，专用实例已关闭；Linux Backup绑定与严格catalog仍BLOCKED。
专项773通过/1跳过、主工程211通过、公开1104通过/42跳过，均exit0；详见
[报告](agent-handoff/PURE_LINUX_BACKUP_DELIVERY_2026-10-28.md)、
[证据](agent-handoff/pure-linux-backup-delivery-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_DELIVERY.md)。以下保留历史状态。

## 2026-10-28：真实 Backup 服务首次入队与重复请求分支

专用 Android ARM64 实例中，自有应用首次请求写 journal、重复请求跳过 journal；
另捕获独立平台作用域的 JobScheduler 提交返回1，交付仍 UNKNOWN，不混同应用因果。
八hook、24事件、零丢失/观察错误；专项725通过/1跳过、主工程211通过、
公开1104通过/42跳过。Linux Backup绑定及严格catalog仍BLOCKED，GameState未运行。
专用5582实例已关闭；旧实例、VM与冻结任务保留。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_SERVICE_RUNTIME_2026-10-28.md)、
[证据](agent-handoff/pure-linux-backup-service-runtime-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_SERVICE.md)。以下保留历史状态。

## 2026-10-28：真实 Backup journal 追加的 ARM64 Linux 局部对照

隔离Android两个真实框架进程与Linux/Bionic两个比较进程均exit0；8条追加84bytes、
另进程重开后9条94bytes逐字节一致。写前/写后fsync已观测，异常边界分别列明。
新增44项工具测试通过，全量专项689通过/1跳过、主工程211通过；不累计重叠集合。
结果为JOURNAL_APPEND_PARITY_LIMITED，尚未绑定Backup服务，严格catalog仍BLOCKED；
本轮未加载libg或推进GameState。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_JOURNAL_RUNTIME_2026-10-28.md)、
[证据](agent-handoff/pure-linux-backup-journal-runtime-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_JOURNAL.md)。以下保留历史状态。

## 2026-10-28：Backup 被动分支观察与 journal/scheduler 边界

隔离AVD的45秒被动窗口未捕获目标自然请求，结果NOT_OBSERVED；六hook完整安装，
无报告丢事件或观察错误，进程身份一致。真实DEX六方法静态审阅确认journal吞IOException、
schedule可能去重返回且忽略底层提交结果；方法返回不等于持久化或交付成功。
新增43项工具测试通过，Linux Backup绑定及严格catalog仍BLOCKED。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_BRANCH_OBSERVATION_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-backup-branch-observation-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_BRANCH.md)。以下保留历史状态。

## 2026-10-28：Backup pending 去重分支与只读队列

真实 Android 只读队列检查确认目标包已有 pending 请求且属于 participants；匹配服务端
DEX 的静态分支显示：put 返回旧请求时跳过 journal，正常路径仍调度 job。
当前未持锁快照不证明历史事务实际分支，journal/交付与 Linux Backup 绑定仍未闭合。
新增25项工具测试通过；隔离全量专项602通过/1跳过、主工程211通过。
严格 catalog 仍 BLOCKED，后续对象图未启动。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_QUEUE_SEMANTICS_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-backup-queue-semantics-evidence-20261028.json)和
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_QUEUE.md)。以下保留历史状态。

## 2026-10-28：loading 双槽真实同步提交与恢复

隔离 Android 实验确认四次同步 commit 均返回 true；目标写入和恢复都按
storage/storage_new 顺序执行，最终两个 XML 与写前逐字节相同，Backup 位于双槽提交后。
新增只读离线审计器；独立副本专项577通过/1跳过，主工程211通过，公开回归1104通过/42跳过。
fsync、journal、跨进程恢复及Linux Backup绑定仍未闭合，严格catalog保持BLOCKED；
ResourceManager/GameState/Step未运行。详见
[报告](agent-handoff/PURE_LINUX_LOADING_DISK_TRANSACTION_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-loading-disk-transaction-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_LOADING_DISK.md)。以下保留历史状态。

## 2026-10-28：loading 观察器修复与真实只读链路

定位旧观察器的作用域过滤漏洞：此前commit零计数应标为未覆盖，不代表没有落盘。
修复嵌套作用域、跨线程磁盘对象关联及事务恢复入口；真实只读探针观察到
`getKeyValue → c.a.j → contains`，同时确认三组偏好文件映射。专项549通过/1跳过，
主工程211通过；异步归因仅单测验证，修复后真实store尚未运行。
严格`CATALOG_READY_PASS`仍BLOCKED，后续对象图未启动。详见
[报告](agent-handoff/PURE_LINUX_LOADING_STORE_PATHS_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-loading-store-paths-evidence-20261028.json)与
[回执](agent-handoff/CODEX_REPLY_2026-10-28_LOADING_SCOPE.md)。以下保留历史记录。

## 2026-10-28：受控 loading 写入事务回归

在隔离 Android 30 ARM64 AVD 上完成一次用户批准的 `LOADING_SCREEN_STATE_LABEL`
写入与原值恢复：客户端和 `system_server` 的 Backup 请求链实际可见，最终值恢复为空。
跨进程磁盘提交、journal、双槽身份和 Backup 交付仍未闭合，因此
`CATALOG_READY_PASS=false`，ResourceManager/GameState/真实 `GameStateStep` 保持
`NOT_RUN`。全量 `tools/pure_linux` 回归为 `531 passed, 1 skipped`，退出码 0；该
结果仅是工程测试，不代表原生生命周期或 no-VM 通过。详见
[本轮回执](agent-handoff/CODEX_REPLY_2026-10-28_LOADING_STORE.md)和
[机器证据](agent-handoff/pure-linux-loading-store-transaction-evidence-20261028.json)。

## 2026-10-28 E2.1-N：Backup callback 服务端语义

历史客户端 ART trace 重分析观察到23次 `requestBackup` 正常返回；匹配 Android 30
服务端 DEX 的11个方法静态调用图显示权限/用户检查、handler入队、journal写入和
备份job调度路径。客户端 Binder 调用是同步事务。参数和返回值未捕获，服务端未运行，
所以真实交付及callback完整契约仍 **UNRESOLVED**；严格 catalog 门保持关闭，
ResourceManager BLOCKED，GameState与真实Step仍 NOT_RUN。详见
[报告](agent-handoff/PURE_LINUX_BACKUP_SEMANTICS_2026-10-28.md)、
[机器证据](agent-handoff/pure-linux-backup-semantics-evidence-20261028.json)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_BACKUP_SEMANTICS.md)。

面向实时、部分可观测对战的 FAIR 搜索、自博弈与策略迭代研究工程。

本仓库保存工程源码、冻结规范、独立实现版本与阶段性验证成果。现阶段重点是基础正确性和迁移验收；正式训练尚未启动。

## 2026-10-28 E2.1-N：双槽持久化接入真实 libg

新增双槽 XML/string-map、历史 ASCII codec 与严格配置桥。独立后端两个 ARM64/Bionic
child 各 48 条参考一致；真实 libg 四 child 完成双槽 commit 后因 backup 未闭合主动 exit96。
独立 NDK 服务查询在返回前因 /dev/binder ENOENT 而 SIGABRT，服务状态仍未知。
专项 482 通过/1 跳过、主工程 211 通过、公开九组 1,104 通过/42 跳过；保留历史 XML。
严格 catalog 及 ResourceManager 仍 **BLOCKED**，GameState/真实 Step/完整局均 NOT_RUN。
当前身份、XML/异常完整域及 backup 契约尚未闭合；详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_BACKEND_2026-10-28.md)及
[机器证据](agent-handoff/pure-linux-preference-backend-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置文件原语与平台审计

新增真实文件分步写入、bak恢复与异常退出验证；两个新ARM64/Bionic子进程
各19组/444项通过，187输入未变。离线核验Android30平台10方法，恢复sync和bak错误语义。
隔离专项409通过/1跳过、主工程211通过、公开九组1,104通过/42跳过；保留248份历史XML。
本轮未加载libg；XML/map、当前身份及真实backup服务仍未闭合，严格catalog保持 **BLOCKED**，
GameState与真实Step仍NOT_RUN。工程测试与原生语义分开记录，详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_FILES_2026-10-28.md)和
[机器证据](agent-handoff/pure-linux-preference-files-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：历史异常恢复与store0字节对照

store1四步padding异常/恢复序列在两份历史原版参考上逐字节匹配；
找到原版历史身份采集记录，store0两份参考各两个ARM64/Bionic子进程、每child30条全匹配。
各187输入未变，本阶段未重新加载libg。XML解析内容恢复不等于整份原字节恢复。
隔离专项378通过/1跳过、主工程211通过、公开九组1,104通过/42跳过，三组分别记录。
当前身份、完整异常及storage/backup仍UNRESOLVED，严格catalog保持 **BLOCKED**，
GameState和真实Step仍NOT_RUN。详见[报告](agent-handoff/PURE_LINUX_PREFERENCE_SEQUENCE_2026-10-28.md)
和[机器证据](agent-handoff/pure-linux-preference-sequence-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：持久Cipher与历史正常路径对照

新增持久AES候选、SHA绑定的身份提取及私有参考工具；两个新ARM64/Bionic子进程
各通过30条历史原版ECB/CBC字节参考，187项输入未变，9个实际映射核验。
专项304通过/1跳过、主工程211通过、公开九组零失败错误；首次映射采集失败保留。
本阶段未重新加载libg。当前身份、异常全域及真实存储/backup仍未闭合，严格catalog
保持 **BLOCKED**，GameState及真实Step仍NOT_RUN。详见
[报告](agent-handoff/PURE_LINUX_PREFERENCE_CIPHER_2026-10-28.md)和
[回执](agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_CIPHER.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：有界初始化图与分步配置语义

真实DEX初始化有界图142节点/15分支相同；完整onCreate仍不同。
新增显式后端C候选，恢复双store、分步commit、异常及backup调用顺序。
当前/历史DEX各4,522例有界差分零差异，保留首次252例差异；
两个ARM64/Bionic候选子进程exit0，未加载libg，外部接口仍为合成边界。
真实密码、平台存储及backup契约仍UNRESOLVED，严格catalog门 **BLOCKED**，
GameState与真实Step保持NOT_RUN。详见[报告](agent-handoff/PURE_LINUX_PREFERENCE_SEMANTICS_2026-10-28.md)
及[回执](agent-handoff/CODEX_REPLY_2026-10-28_PREFERENCE_SEMANTICS.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：配置候选并发保护与初始化身份审计

配置候选加入进程生命周期租约和线程互斥；两个独立ARM64/Bionic实测均通过，
各11条进程回执、187项输入SHA不变。8个配置方法静态摘要相同，但初始化方法不同；
数组载荷相同仍不代表初始化控制流、持久化及backup契约闭合。专项181通过/1条件跳过。
严格catalog门仍 **BLOCKED**，ResourceManager整体门未通过，GameState及真实Step保持NOT_RUN。
详见[本轮报告](agent-handoff/PURE_LINUX_CONFIG_CONTRACT_2026-10-28.md)及
[回执](agent-handoff/CODEX_REPLY_2026-10-28_CONFIG_CONTRACT.md)。以下保留历史状态。

## 2026-10-28 E2.1-N：catalog 自然 ready 与配置依赖纠错

真实 Ubuntu ARM64 的两个新子进程均 exit0，原生 loader 自然推进53步，ready由0变1，
真实加载162张表/24,353行。两次追踪各确认247次update缺失后APK打开、131次update存在后打开。
此前空JavaVM故障已定位为配置写入桥；以下旧报告中的日志桥归因由新证据纠正，原文保留。
单键native持久化候选尚未闭合Android加密、commit及backup外部语义，严格catalog门仍
**BLOCKED**；ResourceManager整体门未通过，GameState及真实Step保持NOT_RUN。
专项157通过/1条件跳过，主工程211通过，公开回归exit0、9组XML零失败错误。
详见[新报告](agent-handoff/PURE_LINUX_CATALOG_CONFIG_2026-10-28.md)及
[机器证据](agent-handoff/pure-linux-catalog-config-evidence-20261028.json)。以下保留历史状态。

## 2026-10-28 E2.1-N：Provider 注册及真实资源读取

Ubuntu ARM64中新子进程完成data/update原生注册和四次完整资源读取，字节SHA、
read/EOF/seek/close通过；catalog两次list158/preload368及分批调用返回，ready仍0。
自然loader在日志路径获取JNIEnv时读取空JavaVM指针而SIGSEGV，整体 **BLOCKED**。
108项专项通过、1项平台条件跳过；主工程211项通过，公开回归9组XML零失败。
GameState/真实Step及完整局未运行，未取得无JavaVM或Android-free结论。
详见[本轮报告](agent-handoff/PURE_LINUX_PROVIDER_CATALOG_2026-10-28.md)。以下保留历史状态。

## 2026-10-27 E2.1-N：真实constructor及内容对象构造

在真实Ubuntu ARM64 guest中，linker记录libg753次constructor进入/返回；
内容root/context和filesystem原生构造均返回exit0，187项输入前后SHA一致。
新增限定结果 **CONSTRUCTOR_TRACE_PASS / CONTENT_CONTEXT_PASS**；ready仍为0，
provider/catalog未接通，整体 **BLOCKED**。GameState、empty battle和真实Step均未运行。
限定JNI/Asset断点计数不代表全路径无依赖；实际观测到Android属性/logdw路径访问。
详见[本轮报告](agent-handoff/PURE_LINUX_NATIVE_INIT_2026-10-27.md)。以下保留历史状态。

## 2026-10-27 新输入续验：真实 libg 已取得 LOAD_ONLY

用户提供的同版本 APK 中，8 个 ARM64 库及全部 9,829 个游戏资源与既有基线逐项 SHA
一致。为该输入单独登记身份，保留历史整包 SHA 差异。在真实 Ubuntu ARM64 guest 中，
匹配 Android linker/Bionic 对真实 libg 执行 dlopen/dlclose，进程 exit0，取得 **LOAD_ONLY**。
全部 187 个暂存输入前后未变；未初始化战斗，未验证无 JavaVM 生命周期、完整对局或10×。
详见[报告](agent-handoff/PURE_LINUX_USER_APK_LOAD_2026-10-27.md)。以下保留历史状态。

## 2026-10-27 续验：ARM64 Linux 与 Bionic 合成 ABI 已实测

经用户授权恢复现有 Ubuntu ARM64 VM，真实 guest 的 Linux 八项 smoke、文件 canary 和
退出码通过，取得 **PURE_LINUX_SMOKE_PASS**；匹配 Android linker/Bionic 的线程/TLS 探针
也退出 0，已核对隔离目录中的 libc/libdl 实际绑定。新增版本符号/relocation 静态审计，
29 项专项通过。初次 clone 探针失败和修复后输出分别保留。

这些结果仅验证合成 ABI，原版 libg 尚未加载：锁定 APK 身份仍缺失，原生内容/manager
初始化依赖仍待验证。no-VM、Android-free、真实完整对局、10× 和训练均未启动。
详见[续验报告](agent-handoff/PURE_LINUX_GUEST_ABI_2026-10-27.md)及
[回执](agent-handoff/CODEX_REPLY_2026-10-27_PURE_LINUX_GUEST.md)。以下保留此前状态。

## 2026-10-27：Apple Silicon → ARM64 Linux 原版引擎审计

任务分支新增匹配 ELF/输入审计及 direct ARM64 Linux、Bionic 探针，15 项专项通过，
两种探针交叉构建成功。214 个运行文件、990 个资源匹配，静态检查 185 个 ARM64 库。
当前 **BLOCKED_ENVIRONMENT**：没有已验证运行中的独立 ARM64 Linux guest，既有挂起 VM 保持原状；
原始锁定 APK 另有身份缺口。guest smoke、真实 libg 加载/生命周期、无 VM 与 10× 均未验证。
详见 [审计回执](agent-handoff/CODEX_REPLY_2026-10-26_PURE_LINUX.md) 与
[ABI 实验](agent-handoff/ARM64_LINUX_ABI_FEASIBILITY.md)。以下保留此前阶段记录。

## 当前状态（2026-10-25）

| 阶段 | 状态 | 尚待完成 |
|---|---|---|
| G0/G1：工程与搜索正确性 | 进行中；限定子项登记 34/43 | 完整 Full128 搜索终局、原生补充轮次与选优、一般原生边界等 |
| G2：正式迁移与 WDL 校准 | 未运行 | 正式 1,280 局数据、校准与非劣验收 |
| G3：搜索增益 | 未运行 | 冻结迁移模型的 128 次搜索实战对照 |
| G4–G8：后续训练与发布门 | 未运行 | 依赖前置门闭合 |

测试通过、限定子项通过、整体门通过和模型实战提升分别记录，测试数量存在重叠，不累加。

新增整轮顺序执行与独立回收：固定准备结果和选优报告，逐局调用既有worker及
独立归档验收器；中断后重验已完成局而不重复采集，失败保留现场。联合117项
工程测试通过、主工程211项通过，集合不相加。尚未执行真实round2，重试调度
及训练交接待完成；G0/G1与34/43不增。Full128最新快照提交185组/370根/
47360模拟、tick5990继续，terminal仍运行。详见
[_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md](_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md)。
以下为此前阶段和时间点记录。

新增整轮补充采集协调预检：任务精确覆盖、输入与外部种子来源一致、输出隔离，
逐任务预检全部通过后才生成准备标记及现有worker命令。32项专项、128项联合
工程回归通过，集合不相加；仅准备，不执行worker或启动引擎。真实terminal、
原生round2及完整搜索局仍待验，G0/G1与34/43不增。首次公开专项因共享测试
依赖私有manifest失败，原报告保留；显式合成计划修复后源联合113项、公开专项
32项均通过且零跳过，完整公开入口通过。Full128快照1792916545.8936303提交
184组/368根/47104模拟、tick5982继续；terminal第18步完成、第19步开始。
详见[_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md](_spec_work/versions/fair-final-collection-coordinator-v1/CONTRACT.md)。
以下为此前阶段和时间点记录。

新增模型格点到原生执行坐标限定验收：62份归档独立重算，三组各426次决策/
20个观察边界及tick5897终局一致；188预约的转换、入队与原生加载坐标全部
一致，其中33个带位置偏移，所属清理通过。最终41项专项通过、零跳过，含
真实归档重验及3,456种冻结转换对照。内部卡牌、完整mask、一般拒绝与全部
随机覆盖仍待验。Full128快照1792915142提交183组/366根/46848模拟继续，
未终局；terminal第18步验证到192/358行。G0/G1及34/43保持开放。详见
[_spec_work/versions/fair-final-command-coordinates-v1/EVIDENCE.md](_spec_work/versions/fair-final-command-coordinates-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增逐预约原生对象映射限定验收：62份归档独立重算，三组各426次决策、20个
观察边界及tick5897终局一致；188预约全部关联构造和真实执行对象，68个双方
同时执行tick、20个对象跨tick复用，所属清理通过。最终33项专项通过，含真实
归档重验。未证明原生命令内部动作字段与坐标转换，一般拒绝、完整mask与全部
随机覆盖仍开放。Full128快照1792913752已提交182组/364根/46592模拟、继续
搜索且未终局；terminal第17步完成、第18步验证中。G0/G1及34/43不变。
详见[_spec_work/versions/fair-final-reservation-map-v1/EVIDENCE.md](_spec_work/versions/fair-final-reservation-map-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增虚分派实际目标限定验收：独立v4插件显式记录X8，三组完整对局各426决策/
20个观察边界及tick5897终局一致；188次调用的目标、对象、参数、LR与返回值
全部配对，62份归档及所属清理通过。最终34项专项通过，含真实归档重算。
只证明本轨迹调用点，逐预约映射、一般拒绝、完整mask与全随机覆盖仍开放。
Full128快照1792911277已提交180组/360根/46080模拟，tick5906继续、未终局；
terminal第17步进行中。G0/G1及34/43不变，未启动正式迁移或放量。详见
[_spec_work/versions/fair-final-dispatch-command-v1/EVIDENCE.md](_spec_work/versions/fair-final-dispatch-command-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增原生动作执行链限定验收：三组完整对局各426次决策、20个周期观察边界
一致，观察到188次ct86执行器与位置helper完整配对，返回码均0；所属清理通过。
两次启动失败保留，过滤版正常终局，最终29项专项通过。未证明一般合法拒绝、
虚分派目标寄存器或完整mask；G0/G1与34/43保持开放。Full128已提交179组/
358根/45824模拟，tick5874继续、未终局；terminal恢复已到第17步，尚待收尾。
详见[_spec_work/versions/fair-final-live-command-v1/EVIDENCE.md](_spec_work/versions/fair-final-live-command-v1/EVIDENCE.md)。
以下为此前阶段及时间点记录。

新增位置拒绝条件的独立CPU核验：原始81条AArch64指令在14,676个合成结构体
样例上与参考公式一致，全部指令及六种返回码覆盖；18项专项通过。另证除500
的全signed32算术公式。合成CPU证据不替代游戏可达性、terrain函数体或完整mask
证明，G0/G1与34/43不变。Full128快照1792907076提交177组/354根/45312模拟，
tick5864继续、未终局；terminal恢复仍运行。详见
[_spec_work/versions/fair-final-position-guards-v1/EVIDENCE.md](_spec_work/versions/fair-final-position-guards-v1/EVIDENCE.md)。
以下均为此前阶段及时间点记录。

本次补齐最早合法延迟的原生有限诊断：四根37组/74次快照续演一致，覆盖
delay2/4/15/18/25及下一档；合法拒绝与单方就绪均0组，独立内容验收及清理通过。
这只补充旧delay1/30探针，未证明一般pending不可达，G0/G1与34/43不变。
Full128快照1792905521已提交176组/352根/45056模拟，tick5804继续、未终局；
terminal恢复仍待验。详见[_spec_work/versions/fair-final-affordability-boundaries-v1/EVIDENCE.md](_spec_work/versions/fair-final-affordability-boundaries-v1/EVIDENCE.md)。
公开回归794通过/27项缺私有附件跳过，独立阈值23通过、恢复与交接82通过、
主工程211通过，集合不累加。以下为此前阶段及时间点记录。

本次完成全局快照搜索的原生限定验收：4根/512有效模拟、2对隐藏干预结果一致，
1483次快照创建与释放、551次恢复及所属双引擎清理通过。完整搜索局仍待验。
新增terminal恢复结果到补充采集包的独立交接，联合82项工程测试通过，含39项
交接测试；真实terminal未完成，真实交接与round2尚未开始。Full128最新快照
提交175组/350根/44800模拟，tick5744继续。G0/G1及34/43保持开放。
详见[_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md](_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md)
及[_spec_work/versions/fair-final-retry-collection-bridge-v1/EVIDENCE.md](_spec_work/versions/fair-final-retry-collection-bridge-v1/EVIDENCE.md)。
发布复核：公开794通过/27项缺私有附件跳过，独立恢复与交接82通过、主工程211
通过；2244项源码清单通过，凭据扫描零发现。下面保留此前阶段与时间点记录。

新增独立terminal超时恢复：旧8小时选优失败保留，第15步完整状态核验通过，
43项联合工程测试通过；24小时续跑已在.35 GPU1启动，真实terminal仍待验。
Full128同局恢复174组/348根/44544模拟至tick5728并继续；独立全局MT搜索
完成1/4根。G0/G1与34/43不变，不启动正式迁移或放量。详见
[_spec_work/versions/fair-final-selection-timeout-v1/EVIDENCE.md](_spec_work/versions/fair-final-selection-timeout-v1/EVIDENCE.md)。
以下为此前测试和运行快照，不作当前存活或准入证明。
本轮公开复核：794通过/27项缺私有附件跳过，独立恢复回归43通过，主工程211通过；
2230项源码清单通过，凭据扫描零发现；测试集合不累加。

新增rng-callsite-audit-v1，本机实际重算18处直接MT初始化参数：11栈相对、
6分配器返回、1传入地址；未知来源保留。25项专项、联合130项通过，不据此
认定对象持久性、固定速猪可达性或快照覆盖。五机SSH仍超时，冻结部署保持。
G0/G1及34/43不变，详见该版本EVIDENCE.md。以下为此前测试和运行记录。
本轮发布复核：公开794通过/27项缺私有附件跳过，2220项清单通过，凭据扫描零发现。

新增global-search-v1独立入口，逐根核对完整native+全局MT摘要、恢复身份与句柄
释放；38项专项、联合139项通过（7.81秒）。真实父证据重验，353项载荷已准备，
新原生Full128实测待执行。五机SSH均超时，原Full128与terminal当前状态待核验，
未因失联重启或修改冻结部署。G0/G1及34/43保持开放，详见
[_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md](_spec_work/versions/fair-final-global-search-v1/EVIDENCE.md)。
发布复核：公开770通过/26项缺私有附件跳过，主工程211通过，2212项清单通过，凭据扫描零发现。
以下运行快照与测试数量均为历史记录。

新增独立QEMU插件测量环境，官方10.0.13构建与33份归档内容核验通过。
修复vCPU槽复用、块内PC误判和跨进程地址绑定；真实合成指令检查通过，
监督封存显式区分正常退出与完整终局后受控关闭。五次原生失败保留，第六次
三组完整局通过限定验收：426条决策、20个观察边界完全一致，44条实际入口
记录及reset正向对照齐全。只证明本轨迹八个已知入口，原始审计私有保留。详见
[_spec_work/versions/fair-final-plugin-runtime-v1/EVIDENCE.md](_spec_work/versions/fair-final-plugin-runtime-v1/EVIDENCE.md)。
联合139项、公开734项通过/24跳过、主工程211项通过，2202项源码清单通过；集合不相加。
Full128快照1792751687：133组/266根/34048模拟提交，tick4834玩家0搜索中；
terminal仍运行，冻结部署与G0/G1、34/43不变。以下为历史进度。

最新工程为已知随机入口的独立执行追踪及内容验收，61项专项通过。四次原生
尝试均失败并保留：规则绑定缺失已修；扩大独立启动等待后仍遇SIGALRM终止，
尚无有效执行日志，不把空日志当作未调用证据。G0/G1与34/43不变，详见
[_spec_work/versions/fair-final-random-execution-v1/EVIDENCE.md](_spec_work/versions/fair-final-random-execution-v1/EVIDENCE.md)。
Full128快照1792744059：127组/254根/32512模拟提交，tick4604玩家0搜索中，
worker及双引擎在线、未终局；terminal选优仍运行。冻结部署不变，下段为历史快照。
本轮2162项源码清单通过，公开回归663通过/17项缺私有附件跳过（47.74秒），
主工程211通过（23.99秒）、联合163通过（8.43秒），凭据扫描零发现；集合不相加。

最新工程补齐：terminal选优结果到补充采集包的独立交接入口，强制父内容验收、
继承冻结训练字节并复核checkpoint身份；35项专项、联合78项通过（58.28秒）。
成功交接为mock工程测试，真实terminal仍运行，真实交接与round2原生待验。
Full128快照1792741500：125组/250根/32000模拟提交，tick4516玩家1搜索中，
未终局；冻结部署、G0/G1与34/43保持原状态。下列快照为历史记录。
本轮2143项源码清单通过，公开回归603通过/16项缺私有库跳过（48.29秒），
主工程211通过（22.40秒），凭据扫描零发现；各集合不相加。

原生开局先验诊断已完成：1024个预注册seed与第二新进程64个seed复测通过
独立验收，17份归档及所属引擎清理通过，跨进程可观察牌序一致。21项事前检验
未发现显著偏离（最小p=0.0772149，阈值0.01/21）；只补有限经验诊断，不证明
精确均匀、全排列支持、隐藏RNG一致或历史条件后验。私有原始审计不公开。
Full128快照1792738671：123组/246根/31488模拟已提交，tick4470玩家1搜索中，
无失败、未终局；terminal选优仍运行，冻结部署与G0/G1、34/43保持原状态。
本轮2140项源码清单通过，公开回归584通过/16项缺私有库跳过（49.01秒），
主工程211通过（24.21秒），
凭据扫描零发现；各集合不相加。以下段落保留此前快照。

新增条件分析：真实history-v2运行时对1,981种时间与最早到期结果组合核对通过。
961种无拒绝组合保持双方同步；928种较早ACT私有拒绝组合产生单方就绪。
结果环境为合成值，只证明条件时序关系；合法原生拒绝可达性仍待证。
11项专项、联合45项通过，G0/G1与34/43不变，Full128冻结部署保持原样。

最新补证：双独立原生进程完成有战斗的预约快照续演。tick110/600/1200六个根
均含双方pending预约，每根两条各60tick战斗分支逐tick一致，完整根恢复通过。
每进程双方实际ACT14/13次、正常取消5次，实体/公开血损证据齐全；21份归档
独立验收，句柄和所属引擎清理通过。55项专项、联合240项工程测试通过（20.67秒）。
全局MT只读观察未变化，不证明固定速猪消费者不可达或全部随机状态覆盖。
Full128快照1792736248：121组/242根/30976模拟，tick4392搜索中、未终局；
开局采样801/1024，terminal选优待验。冻结部署及34/43不变，G0/G1保持开放。
发布复核：2131项源码清单通过，公开回归573通过/16项缺私有库跳过（72.25秒），
主工程211通过（36.70秒），凭据扫描零发现；各集合不相加。

此前独立修复：单比赛执行通道的全局MT快照与摘要覆盖通过限定验收。
两个新原生进程、8个边界全部恢复2504字节，重复restore及两条各10步WAIT
续演一致；新摘要感知全部受控扰动，19份归档独立校验。新47项与随机诊断
联合185项通过（11.37秒）。固定速猪实际消费者和有战斗续演仍待证，
不将单对象覆盖当全部随机门通过。旧引擎与后台冻结部署保持原样。
Full128快照1792734434提交119组/238根/30464模拟至tick4322，tick4342继续；
开局545/1024、terminal选优运行。G0/G1与34/43不变，G2–G8未启动。
发布复核：2118项源清单通过，公开回归518通过/16项缺私有库跳过（62.13秒），
主工程211通过（36.07秒），凭据扫描零发现；各测试集合不相加。

此前原生诊断：全局MT的加载注册入口及三段消费者代码已绑定。
两个独立引擎、共8个边界均证实该对象的受控扰动未被快照恢复，state-digest
也未检测其变化；修复原字节和所属引擎清理通过。WAIT轨迹中它保持不变，
尚未证明固定速猪战斗实际使用或受其影响，不宣称已发现实战非确定性。
新增33项，与alias/shuffle/seed联合138项通过（6.06秒），原始内存审计私有保留。
Full128快照1792733078提交118组/236根/30208有效模拟至tick4302，tick4322继续；
开局369/1024，terminal选优仍运行。G0/G1及34/43开放，冻结部署保持原样。
本轮发布复核：2106项源清单通过，公开回归471通过/16项缺私有库跳过
（39.82秒），主工程211通过（23.46秒），凭据扫描零发现；各集合不相加。

此前补查：随机初始化直接引用之外，新增无条件跳转入口闭包审计。
18处直接引用扩展为19条边，定位额外time(NULL) seed路径及全局对象；
实际入口可达性、固定速猪用途和快照覆盖仍待证，未发现已证实的实战影响。
新增31项，与洗牌/种子联合105项通过（5.37秒），冻结部署保持。
Full128快照1792731888已提交117组/234根/29952有效模拟，tick4302继续搜索、未终局；
开局采样177/1024，terminal选优运行。G0/G1及34/43保持开放，未五机放量。
本轮发布检查：2094项源清单通过，公开回归439通过/15项缺私有库跳过
（38.08秒），主工程211通过（22.14秒），凭据扫描零发现；测试集合不相加。

上一阶段：基于fetch确认的主线`18e984c`，active独立恢复重试通过双归档内容验收。
失败父保持失败，子恢复第5→7步；网络、AdamW、RNG、采样游标及选优状态一致。
此验收未重新核验原始对局来源，不代表G2或训练准入。独立terminal选优已启动，
结果待验；本地派发公开路径边界修复32项测试通过，未改运行中的冻结部署。
独立1024seed开局采样与新进程64seed复测已启动，34项工程测试通过；
原生采样未完成，21个事前检验尚无结论，不代替历史条件后验或全部随机机制覆盖。
Full128已提交116组/232根/29696有效模拟至tick4266，tick4294继续搜索，未终局。
G0/G1与34/43登记保持开放，G2–G8及五机放量未启动。
本轮复核：2088项公开源码清单通过，公开回归414通过/9项缺私有库跳过
（37.95秒），主工程211通过（21.97秒），凭据扫描零发现；各集合不累加。

此前补证：基于fetch确认的主线`11bc2ac`，完成手牌临时随机路径静态审计。
14段字节摘要、18处MT初始化直接引用函数归属及正常返回清理控制流通过；
手牌临时生成器的seed来源与旧tick-seeded debug路径分别定位，不把静态引用
当作实际可达性。新24项与旧seed50项联合74项通过、零跳过，3.79秒。
其余16处引用、间接分派、原生路径观测和先验校准仍待证，G0/G1与34/43保持开放。
后台快照：114组/228根/29184有效模拟至tick4170，tick4230搜索中、未终局；
active重试仍运行，terminal选优未启动。冻结部署保持原样。
发布复核：2075项公开源码清单通过；公开回归348通过、9项缺私有库跳过
（38.62秒），主工程211通过（23.99秒），凭据扫描零发现。各集合不累加。

此前基于主线`f0707ee`，完成原生种子生命周期限定验证。
两个独立引擎、各四阶段的快照续演与同seed状态一致，同进程重复reset一致，
所属引擎清理通过。50项本机测试通过；原失败证据保留，未改变冻结运行部署。
首次reset前时间缓存已填充，本次seed增量均0；未覆盖全部随机组件或原生增量1。
Full128仍在.35后台运行：112组/224根/28672有效模拟至tick4106，未终局；
active恢复重试仍运行，terminal选优未启动。G0/G1与34/43保持开放。
本轮2067项公开源清单通过；公开回归331通过、2项缺私有库跳过（57.77秒），
主工程211通过（36.01秒），凭据扫描零发现。各集合不累加。

上一阶段核验：fetch全部远端分支确认更新前最新主线为`416c616`，已有审计分支已整合。
当时2056项公开源清单通过；公开工程回归282项通过、1项因私有游戏库缺失跳过，
主工程211项通过。新静态参数审计的本机15项通过，其中真实固定库检查仅在本机运行；
不把工程测试或缺少附件的公开副本当作原生完整局验收。

2026-10-23 Full128优先推进：旧段在tick3922保存，103组、206根、26,368模拟的
递归父链与244项冻结源码已核验。用户指定新段24小时，启动、worker、探针及
验收同步支持86400秒，保留30分钟保存余量；51项专项通过9.16秒。
同局新段已在.35启动，新进程已重演211条记录至tick3922并开始新根，
完整终局及最终独立验收仍待证据。active父最后
trainable恢复阶段超总时限exit124，原证据保留，整体门与34/43保持开放。
后台运行期间，一般provider原生local四根512有效模拟已独立验收通过：
tick110/142两组隐藏状态配对一致，增量锚点、模型身份与所属双引擎清理通过。
它不覆盖原生pending/fallback或总体先验校准。队列mode合法WAIT轨迹已观察到
bit0-set；进一步静态核对ct86保存参数只显式读取bit1，不证明整个执行器等价或
合法拒绝不可达。快照1792718966：Full128提交105组至tick3982，tick4036搜索中，
双引擎在线且无失败报告。中心只读空间206.93GiB，两代正式数据容量仍未验。

后台继续期间，独立解冻恢复重试已在.35 GPU1启动，原超时父仍为失败。
新增双归档验收与显式重试链选优交接，保留冻结实现、完整恢复状态与原门槛；
缺链、身份复用及篡改拒收。真实重试仍运行，terminal选优尚未启动。
恢复与选优交接联合工程测试141项通过，182.68秒，不替代原生恢复验收。
快照1792720941：Full128已提交107组/214根/27392模拟至tick4038、未终局。
随机状态全覆盖仍有缺口；G0/G1开放、34/43保持，不开展五机放量。

## 目录与入口

- [`firstlight_CR/`](firstlight_CR/)：主工程，包含原生适配、策略模型、旧 PPO 基线、FAIR 搜索及测试。
- [`agent-handoff/`](agent-handoff/)：ChatGPT ↔ Codex 专用文字交接：迁移步骤、代码审查、验收证据与回复模板；不属于运行代码。
- [`_spec_work/versions/`](_spec_work/versions/)：隔离的增量实现、回归与证据，包括历史信念、一般退化路径、迁移消费、恢复、CUDA 推理和原生队列审计。
- [`冻结规范`](_spec_work/evidence/fair-final-v1-20261019/spec/clash_royale_fair_final_v1/README.md)：训练语义、数据契约、验收门及实验矩阵。
- [`整体进度`](_spec_work/evidence/fair-final-v1-20261019/DOCUMENT_EXECUTION_PROGRESS.md)与[`闭合清单`](_spec_work/evidence/fair-final-v1-20261019/G0_G1_CLOSURE_CHECKLIST.md)：完整历史记录。
- [`成果索引`](docs/RESULTS.md)、[`复现说明`](docs/REPRODUCING.md)、[`发布边界`](docs/PUBLICATION.md)。

部分历史文档使用本机绝对路径和旧机器部署目录，保留为历史上下文；本仓库入口以相对路径为准。`firstlight_CR` 的历史介绍含旧模型发行说明，本公开快照的实际附件范围以 `publication/source-manifest.json` 为准。

## 快速验证

使用 Python 3.12 或 3.13。先创建独立虚拟环境，安装测试依赖；CPU 版 PyTorch 足以运行下面的工程测试。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python tools/check_public_snapshot.py
.venv/bin/python tools/run_public_checks.py
python tools/run_main_engine_checks.py    # 另测当前 firstlight_CR；不替代冻结历史版本
```

原生对局、完整训练及静态二进制审计需要自行准备匹配的游戏引擎、资源及模型。仓库附有适配源码、构建输入源码和身份摘要，第三方引擎二进制、提取资源、私有对局及模型权重单独保留。详见复现说明。

## 目标完成后的更新

每个目标完成后同步相应实现、测试、文档和证据摘要，运行公开检查后提交并推送本仓库。更新流程与凭据处理要求见 [`AGENTS.md`](AGENTS.md) 和 [`维护流程`](docs/MAINTENANCE.md)。长时间运行的任务继续使用其冻结部署，仓库同步独立进行。

沿用原工程的 [Apache-2.0](LICENSE) 与 [NOTICE](NOTICE)；第三方材料另受各自条款约束。
