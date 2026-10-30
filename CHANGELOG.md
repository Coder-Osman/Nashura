# 变更记录

## 2026-10-31：原生动作与对象更新诊断

- 增加八处精确 guard、保留 BL/尾跳语义的调用点诊断，以及近分支岛和六窗口嵌套计数；默认关闭，禁止混入 benchmark 或 native 优化。
- 新汇总器验证完整终局、解析/入队、队列守恒及 native 计数；显式记录时钟量化的零时长样本，不解释为零成本。
- 四组固定卡组/seed 共 12 局全帧及已观测状态一致，主机独立复算；取得真实 mode 3 dispatch 和对象集合归因，没有新增加速结论。
- 保留 v1 部署失败、v2 安装/汇总失败与 v3 成功证据；固定 VM 配置，不测并发、不扩张 Android 服务，整体目标继续进行。

## 2026-10-31：热 getter 内联的限定串行收益

- 新增默认关闭的 `hot-getters-v1`：11 个 BL 替换为原 getter 的同宽/同偏移 LDR，完整字节 guard、RW/RX 和指令缓存刷新，与空间候选互斥。
- 四组多卡组/seed 完整局在新默认路径、优化路径和历史原始路径间全帧及已观测状态一致；主机复算原始归档与日志。
- 同 observer ABBA/BAAB 32 批、3712 局正常退出；四组耗时降低约 5.00%–5.89%，聚合吞吐提高 5.77%–6.46%，保留四批/variant 的统计边界。
- pure_linux 1249 passed / 1 skipped、公开 1104 passed / 42 skipped、main_engine 211 passed；候选保持 opt-in，未测并发或修改 VM 配置。
- 记录当前 Mac Fusion ARM64 guest 无 QEMU 翻译层及仍依赖 ART/JNI 的边界；整体优化目标继续进行。

## 2026-10-31：多卡组 serializer 回归门与 native 查询候选

- 新增隔离的原生手牌动作记录、严格冻结器及两卡组/两 seed 固定输入，禁止记录模式混入 benchmark。
- 四组完整局逐 tick serializer、已观测状态以及新构建默认路径均与原始路径一致。
- 增加固定 build 的 opt-in 空间查询分支候选；仅进程内四字节、精确字节 guard、RW/RX 与指令缓存刷新，默认关闭。
- 同 observer A/B/B/A 16 批、1,856 局全部完成；小幅耗时差异尚未建立稳定性能收益，不将局部等价写作全引擎等价。
- pure_linux 1242 passed / 1 skipped、公开 1104 passed / 42 skipped、main_engine 211 passed；保留部署失败及原始证据，整体目标仍进行中。

## 2026-10-31：warm-step profile 窗口归因与长串行等待

- 修复 benchmark 共用单个 Mainloop task 时触发默认约五秒等待的问题；等待跟随 timeout，保留原生 120 秒上限及独立外层终止界限。
- 新增严格 warm-window perf 汇总器，验证 monotonic clockid、退出码、完整终局、输入身份、最小观测及零丢样；按进程/时间映射归一化 libg 文件偏移。
- ARM64 guest 216 局 profile 正常完成，后 200 局保留 5,689 个窗口内 CPU-clock 样本；libg 叶样本占 98.95%，空间查询范围占 16.68%。
- 同构建两批独立无 profiler 基线各测 200 局，中位 29.43 / 29.02 ms；不解释为优化前后收益，保留单 seed/卡组及终态摘要边界。
- 旧 CLI 与长批次 task timeout 失败原始文件继续保留；没有修改 libg、扩大 Android 服务适配或运行并发测试。整体目标仍未完成。

## 2026-10-31：串行 step 采样相位复核

- native benchmark 在串行 reset 间轮转采样相位，记录 `sample_phase` 与 period，并在汇总器中核对相位、采样数和动作序列契约。
- ARM64 guest 16 局预热加 30 局测量，全部 46 局复现相同 tick 3601 终态；6752 个 step 样本覆盖全部 16 个相位。
- 当前中位每局 68.67 ms；与旧记录差异明显且 observer/计时实现不同，不据此宣称加速；本轮没有改引擎或测并发。
- Python 3.13.5 下纯 Linux `1189 passed、1 skipped`、公开检查 `1104 passed、42 skipped`、主引擎 `211 passed`；Python 3.9 的依赖缺失失败单独留痕。
- 公开源码清单 2,885 项、gitleaks 与 `git diff --check` 通过。
- 多卡组完整对局、优化前后配对及整体 readiness 仍待完成。

## 2026-10-30：温热串行 step 基线与 seed 复验

- 新增 fixture 专属 serializer 字节长度参数和终局身份参数校验，支持严格汇总随机终局。
- ARM64 Linux 单进程串行 30 局基线完成；每 16 tick 分层采样，共 6,780 个 step 样本，终态摘要校验通过。
- 同一 52 命令/八卡组固定动作以 seed 7/19/41 各串行复跑 30 局，三组均稳定达到原生终局，终态 digest/长度各自一致。
- 补充 RUSAGE_CHILDREN CPU、缺页和上下文切换计数；perf 需 sudo 且 guest 非交互认证未通过，未取得新调用栈 profile。
- 本轮没有测并发，也没有应用引擎优化；只有一个有效卡组 fixture，优化收益、更多卡组与整体 readiness 仍未验证。

## 2026-10-30：空间查询调用者静态审计

- 依据与 profile SHA 相同的 ARM64 ELF，关联二维网格查询包装器、主体和一个包含调用点的上层函数。
- 新增多调用点证据，确认查询机制可复用；玩法具体用途、虚回调身份及动态调用者仍保持未知。
- 本轮只做静态反汇编，没有执行 replay、并发或性能测量，没有改动第三方二进制。
- 整体 headless 目标和优化收益均未验证。

## 2026-10-30：ARM64 headless 串行 profile 热点归属

- 关联 ARM64 guest 映射、精确 `libg.so` SHA、DWARF unwind 边界和 AArch64 指令，定位 `+0xf34b3c..+0xf35064`。
- 该范围在 2,569 个软件 CPU 时钟样本中命中 294 个；代码呈现定点二维格遍历、候选对象过滤和虚调用。
- 将“空间格候选查询”记录为行为推断，保留具体玩法语义未知；没有修改或分发第三方引擎二进制。
- 离线复算保留的 profile script/maps：2,569 个 sample IP 可解析，查询范围命中 294 个且全部来自 Mainloop；更正占比为 11.44%。未重跑 workload，raw perf.data 因 guest 文件权限未取回。
- 保留单 fixture、单 guest、串行 profile 的边界；未测并发、多 seed/卡组，也未执行优化。
- Python 3.13.5 下纯 Linux `1183 passed、1 skipped`，公开检查 `1104 passed、42 skipped`，主引擎 `211 passed`；快照清单与 gitleaks 均通过。
- 整体目标继续未完成。

## 2026-10-30：Headless replay 阶段诊断

- 增加默认关闭的阶段计时和战斗状态观测；增加关闭逐 tick snapshot trace 的 runner 选项。
- 原始 replay 与 52 条命令注入 replay 均在固定 ARM64 fixture 到 tick 3601 原生终局，终态摘要相同。
- 730 条历史 v38 observation/player/object 记录逐行匹配；队列 52→2，50 条命令在 31 个变化步中消费。
- 单次阶段耗时仅作诊断；未测并发、未建立性能基线，整体 ready 继续未验证。
- 发布脱敏机器摘要与报告，不发布 observer、原始 guest 日志或第三方运行时资源。

## 2026-10-30：替代转译器真实对局对比

- 排除FEX方向不符；实际构建arm64emu-user x86_64 JIT和上游QEMU11.1.3。
- 两候选3602帧归档匹配；20/30局正常终局，原QEMU相同observer复测30局。
- 中位2.168/3.107/4.858s，保留原QEMU；不同版本和构建配置不作单因果结论。
- 公开自有guest直接读取补丁及失败摘要，缺失smaps输出null；统计器14项通过。
- 最终纯Linux1176 passed/1 skipped；保留缺cryptography的首次环境失败。

## 2026-10-30：x86服务器效率对比与内容身份

- 相同两核顺序完成ARM Linux QEMU及Android native bridge各30局，保留首轮争用样本。
- 两条服务器路线3602帧配对；报告step、批次总耗时、进程与VM内存的不同口径。
- 新增Android最小ClassLoader语义、native bridge诊断边界及QEMU真实pthread入口。
- 新增完整局统计校验器和13项测试；同源计时/计算探针限制跨机性能归因。
- 五阶段内容对象图身份在ARM guest真实对局及相同归档中通过；整体ready仍未验证。
- 纯Linux1175 passed/1 skipped、公开1104 passed/42 skipped、主工程211 passed。

## 2026-10-30：零Binder直接ART入口

- 新增真实AndroidRuntime直接入口及原始sigchain构建身份，省去启动器Binder线程池。
- 零Binder设备/零Binder ioctl下完整replay与Android3602帧配对；可复用runner真实通过。
- 屏蔽oldroot/sys，保留全局policy、传递库闭包和snapshot adapter限制；1162项回归通过。

## 2026-10-30：真实首局、固定动作复演与快照适配

- 新增版本绑定Mainloop任务桥、最小x8 ABI assembly桥与真实GameState探针。
- 修正重复入队和空可部署卡组；selection由原始函数生成，保留真实验证与apply。
- ARM64 Linux首局tick3601原生终局；52条固定命令复演，采样状态一致。
- 保留原始snapshot空controller崩溃；显式原始serializer适配完成交战中恢复、
  续演与两次同进程重建一致性；适配覆盖范围由同版本Android逐tick配对限定。
- 撤回v35h2重复命令及漏ICU启动失败的过度解释，旧失败证据继续保留。

- 新增独立namespace入口，裁剪根设备视图后完整replay仍与Android逐tick一致；
  Binder启动前置和历史SELinux可见性保持显式限制。

- 新增逐tick快照严格比较器及10项负向/完整性测试；同版本Android与Linux
  3602个serializer payload字节一致；完成单进程及1/2/4进程限定性能对照。

## 2026-10-30：最小 headless 原生生命周期

- 新增 APK 局部 Context、显式失败的 PackageManager 生成器及原始 JNI 生命周期探针；
  Resources、HPE 查询和本地 Intent 均按真实调用缺口补入，并记录固定 fixture 边界。
- 新增进程内日志、JNI 注册观察及 Mainloop PC/LR/帧链采样；修正 ARM64 顶字节标签
  导致的内存读取误判，区分 null、读取失败与空父槽。
- v17 在真实 ARM64 Linux 完成原始 native 生命周期并自然退出0；holder/game 已建立，
  内容 root 仍为空，战斗初始化与 reset/step 尚未打通。
- 正式纠偏系统/图形服务路线：历史服务依赖不作为战斗必需证据，保留失败与误读历史，
  后续沿内容与战斗调用链推进。

## 2026-10-30：Display statsd linker 依赖复验

- 扩展纯 Linux namespace launcher，允许图形服务输入及 vendor 图形根只读 bind，并
  为服务子进程传递固定补充组；`namespace_exec` 增加数值补充组解析和降权路径。
- 将同一 statsd APEX 的 `libstatssocket.so`、`libstatspull.so` 固定到
  `/system/lib64`，在 probe 前检查同源哈希，拒绝缺失、身份错误或非同源库。
- 在真实 ARM64 guest 完成 r5 复验：两库均被 SurfaceFlinger/GPUService 从
  `/system/lib64` 打开；三次 property 读回、输入身份和 sysctl 恢复通过。
- 保留 SurfaceFlinger 的 EGL/goldfish pipe 缺口、composer framebuffer 缺口和
  allocator 退出证据；`system_services_ready=false`，完整系统服务与游戏验收门保持关闭。
- 私有 guest 输出归档已复制并清理；公开仓库只加入脱敏报告、证据摘要和源码，未加入
  凭据、policy、DEX、第三方 ELF 或原始 strace。

## 2026-10-30：Display 服务依赖边界

- 新增 v25 原始 init Display probe 的脱敏报告、机器摘要和 Codex 回执。
- 记录 service-v2 工具链下 5 个 bootstrap APEX 挂载成功、property 三次独立读回、loop cleanup、输入身份和 sysctl 恢复。
- 记录 Display 依赖链到达后因缺少 `SurfaceFlinger` 在 `/dev/binder` 等待；`system_services_ready=false`，完整 system services 和游戏验收门保持关闭。
- 原始 policy、DEX、第三方 ELF、strace、guest 文件和凭据未进入公开仓库。

## 2026-10-30：ART 服务观察器 guest 限定运行

- 扩展 `ArtSystemServicesProbe`，增加 Thermal/Power Binder 与服务列表、ServiceRegistry fetcher、Context 缓存槽、`PowerManagerInternal` 观察和阶段化错误输出。
- 标记 fetcher 复查对隔离 Context 缓存槽的干预；Java 17 严格编译与专项测试通过。guest 中原始 Power/Thermal、Binder、fetcher、Context 和 PowerManagerInternal 的 13 项观测完整，probe exit 0。
- v22 apply 中三次 property 写入与读回通过，输入身份不变、三项 sysctl 恢复；policy 仍为 permissive 且已加载。服务状态限定为 `ART_SERVICE_FETCHER_TRACE_RUNTIME_OBSERVED_LIMITED`，完整 system services readiness 与游戏链路保持未验收。
- 发布脱敏报告和证据摘要；原始 DEX、policy、运行日志与 ART 归档保留在私有临时目录。

## 2026-10-29：原始 APEX 激活与持续属性服务

- 将同镜像 22 个原始 APEX 作为只读输入，原始 apexd 逐包校验并实际挂载五个 bootstrap APEX；按真实挂载事件计数，汇总行的 22 不作为挂载数。
- 原始 BoringSSL 64 位自检返回 0，init 随后等待真实 coldboot；三次属性独立读回通过。v14 启动原始 hwservicemanager 与 suspend HAL，观察到 `ISystemSuspend/default` 注册；HAL 受 guest 电源 sysfs 缺口限制，DisplayManagerService 因 PowerManager 对象为空而失败。
- 新增按 backing device/inode 核验的 loop 分配与清理工具；保留首次路径检查失败及专门恢复证据，最终新设备全部移除、原设备状态一致、sysctl 恢复。
- 纯 Linux 专项回归 `1138 passed、1 skipped`，主引擎 `211 passed`；发布 `ORIGINAL_APEX_BOOTSTRAP_PASS_LIMITED` 报告与证据，系统服务、GameApp 和完整游戏链路继续未验收。

## 2026-10-29：原始 init 属性写入与读回

- 用户确认的 guest policy 实验取得真实 property ACK 与 getprop 读回；连续写入三个值验证通过。
- 新增只复用固定已加载 policy 的计划/执行入口，验证输入身份、隔离 namespace、恢复三项 sysctl。
- 保留真实 SELinux 计算接口并保护配置节点；新增 PT_INTERP 执行 helper、服务子进程降权和独立 devpts。
- 记录 init 的 APEX tmpfs、原始可更新 APEX 缺口及 BoringSSL 自检退出；flattened 目录实验失败也保留。
- 系统服务和游戏验收门保持关闭，发布脱敏证据及 1133 项纯 Linux 工程复验结果。

## 2026-10-29：android_servers 加载与系统服务依赖

- 定位 JavaVM 创建时的 libart.so namespace 缺口，补齐精确导出并支持独立配置 bind；公开生成配置已在 ARM64 guest 复测。
- 修正真实 SystemServiceManager 构造器的反射访问，按依赖顺序尝试原始 PowerManagerService / DisplayManagerService。
- 以子进程 RLIMIT_NICE 解决原始线程优先级许可；记录 PowerManager 缓存属性写入的真实失败与五次输入一致性。
- 保留失败归档，新增逐文件验证的 ART 回退归档；本组服务依赖实验未改变全局 policy / sysctl，游戏验收门保持关闭。

## 2026-10-29：原始 init property service 请求边界

- 扩展 namespace launcher，为原始 init 提供独立 property/socket/log 输出及原始 vendor 输入。
- 新增有输入身份和状态对照的真实 property 协议探针；三次复测均收到权限拒绝 24，init 后续 fatal 退出 6，保留原始失败证据。
- 增加协议分段/截断、输出路径和验收门测试；socket 或 ACK 不等于 property readback、系统服务或游戏初始化通过。
- 发布限定阶段报告与脱敏 SHA 摘要，系统服务和完整游戏链路继续推进。

## 2026-10-29：真实系统服务启动边界

- 新增原始 `SystemServer`/`android_servers` 入口探针和脱敏证据摘要。
- 记录真实 property service 缺失、同镜像 ADB pairing 依赖以及补依赖后的 linker 边界；
  display/package、GameApp Context、catalog、ResourceManager、GameState、Step 和完整对局继续未验收。

## 2026-10-29：真实 ART 下的 holder setup 生命周期

- 修正 holder payload 与 allocation 前缀的对象布局断言，真实偏移为 `24`。
- 在原始 APK DEX 的 ARM64 ART/binderfs 运行时验证 native setup return、manager 替换、holder 析构、provider release 和 `dlclose`。
- 记录 setup 后 provider 路径清空、`GameApp` Context 空引用及 framework boot classpath 边界；严格 catalog、ResourceManager、GameState、Step、完整对局和性能保持未验收。
- 扩展 holder setup 审计器与测试，发布脱敏报告和证据摘要。
- 以 Android v4 原始追踪脚本身份完成 Android/Linux holder 跨端审计，校验 setup 事件关联、Linux 输出顺序及输入/资源/辅助文件/probe 前后身份；状态保持 `NATIVE_VERIFIED_LIMITED`。
- 修正审计器读取运行器 result 顶层 update inventory 的层级，并增加对应回归测试；审计输出与失败身份均保留。
- 在 guest 一次性 SELinux 启动、真实 binderfs 和同镜像 `servicemanager` 下复跑 Context：`ActivityThread.systemMain()` 返回 `android.app.ContextImpl`，`IServiceManager` descriptor/ping 通过；display/package 服务与 `GameApp` Context 仍未初始化，记录为 `SYSTEM_CONTEXT_PASS_LIMITED`。

## 2026-10-29：真实 ART/JavaVM、JNI 生命周期和 libg VM 绑定

- 真实 binderfs 协议 8、1 MiB 映射与 ioctl 通过；直接从 SDK 镜像重建 ART 闭包，全程未启动 Android 模拟器。
- 修正 boot classpath 顺序、APEX namespace 名称、重复 bionic TLS 与 statsd 搜索路径，Java 计算和 JNI pthread 生命周期真实通过。
- 新增身份检查的引擎 Java fixture 和只读 VM observer；`libg.so` 的 VM 槽、环境 getter 均与 ART 匹配，进程 exit 0。
- 保留属性 SELinux `EPERM`、配置失败链及 15 个游戏 Java 类缺失；holder setup、严格 catalog、ResourceManager、GameState、Step、完整对局和性能仍待验收。
- 发布配置/namespace/property/JNI 源码、复现文档与证据摘要；只更新当前工作分支，保持 main 不变。

## 2026-10-29：Android ART 闭包与 app_process Binder 阻塞

- 在同构 Ubuntu ARM64 guest 的 user/mount namespace 中重建含 boot image 的 ART 闭包；`/proc` bind mount、`pivot_root` 和 linker NEEDED 解析通过。
- `app_process64` 已加载 `libandroid_runtime.so`、`libart.so`，选择 default boot image 配置并进入 `JNI_CreateJavaVM`；启动随后在 `/dev/binder` 缺失（`ENOENT`，占位普通文件对照为 `ENOTTY`）处以 `SIGABRT`、退出码134结束。
- 阶段状态为 `BLOCKED_GUEST_BINDER`；没有建立真实 Android JavaVM，Linux holder setup 继续为 `RUNTIME_BLOCKED_JAVAVM`，catalog、ResourceManager、GameState、Step、完整对局和性能保持 `NOT_RUN`。
- 详见 `agent-handoff/ANDROID_ART_CLOSURE_APP_PROCESS_BINDER_BLOCK_2026-10-29.md` 及对应 JSON 证据摘要。

## 2026-10-29：Linux holder setup 真实执行与 JavaVM 阻塞

- 恢复 VMware guest 登录后，使用新 probe 真实执行 `--holder-setup-only`；内容 root、filesystem 和两个 provider 注册返回。
- GDB 观测将首个 fault 定位到 `libg.so+0x17d8b14` 的空 `JavaVM` 解引用；输入、资源、辅助文件和 probe 身份均保持不变。
- 新增 `PURE_LINUX_HOLDER_SETUP_JAVAVM_BLOCK_2026-10-29.md` 及机器证据摘要；严格 catalog、ResourceManager、GameState、Step、完整对局和性能继续保持未运行。

## 2026-10-29：worker2 后续工程复验

- 使用 Python 3.13.5 重跑公开工程门：纯 Linux `1105 passed、1 skipped`，公开主组 `794 passed、27 skipped`，主引擎 `211 passed`，其余分组均通过。
- `check_public_snapshot.py` 和 `gitleaks` 通过；Python 3.9 的收集错误保留为环境边界，不覆盖正确解释器结果。
- guest SSH 仍拒绝，Linux holder setup、严格 catalog、ResourceManager、GameState、Step、完整对局和性能保持 `BLOCKED/NOT_RUN`。
- 详见 `agent-handoff/CODEX_REPLY_2026-10-29_REVALIDATION.md`。

## 2026-10-29：Linux 原生持有者设置阶段

- 新增 `--holder-setup-only` 及 Linux holder setup 的对象槽、分配器关系、析构清理和输出顺序检查。
- 新增 `audit_native_holder_setup.py` 及定向测试；纯 Linux 专项 `1105 passed、1 skipped`。
- Android holder setup 证据稳定通过；Ubuntu ARM64 guest 因 `vmrun` 缺少有效登录密码且 SSH 22 端口拒绝，Linux setup 记录为 `BLOCKED_ENVIRONMENT`，不把未执行结果写成通过。
- 详见 `agent-handoff/PURE_LINUX_HOLDER_SETUP_2026-10-29.md` 与对应机器证据摘要。

## 2026-10-29：Linux 原生持有者工厂生命周期

- 在匹配真实 `libg.so` 的 ARM64 Linux 隔离子进程中完成参数工厂和持有者工厂的创建、字段检查、析构及分配器释放；跨端身份审计 exit 0。
- 本阶段状态为 `NATIVE_VERIFIED_LIMITED`，相关测试1094 passed、1 skipped；严格 `CATALOG_READY_PASS` 仍为 `false`。
- 配置异常、Android Backup 等价、ResourceManager、GameState、Step、完整对局和性能验收保持未运行；原始日志、库和资源未发布。
- 详见 `agent-handoff/PURE_LINUX_NATIVE_HOLDER_FACTORY_2026-10-29.md` 与对应机器证据。

## 2026-10-29：完成函数查询键对照

- 双端真实调用的30字节查询键一致，返回值仍有非空与空的差异。
- 保留错误字符串布局的失败证据；查询对象初始化和严格catalog仍未闭合。

## 2026-10-29：catalog 完成函数查询分歧

- 匹配引擎的隔离 Ghidra 分析与双端真实观测定位候选查询返回差异。
- 两侧context槽均非空；查询键、对象初始化及严格catalog仍待验证。

## 2026-10-29：catalog 完成调用前后计数

- 双端观察第5批两个候选调用，首次计数差异位于 `finish_a` 进入至返回区间。
- 并发影响和具体内部因果未知，严格 catalog 仍 BLOCKED。

## 2026-10-29：catalog 批次前后计数

- 双端对齐 908 项 update，读取17个差异槽的每批计数并定位第5批首次分歧。
- 批次内部调用、最终增长及严格 catalog 仍待调查。

## 2026-10-29：匹配 update 的 catalog 布局

- 逐项哈希复制 908 项资源并运行真实 Linux libg，与清单稳定的 Android 样本对照。
- 两侧仍差 17 槽、1,457 行；已覆盖 Linux 布局在 990→908 对照中不变。

## 2026-10-29：catalog 批次返回序列

- 加入独立 AVD 启动门控及脚本异常捕获，修复只读回调字段赋值。
- 双端 313 次 getter 序列匹配；重新验证 597 项资源对象类别，严格 catalog 仍 BLOCKED。

## 2026-10-29：catalog 批次作用域观测

- 加入按线程启用的 GDB 作用域并验证 Linux 真实入口返回；首轮超时证据保留。
- Android attach 超时，未取得跨平台比较；严格 catalog 门保持关闭。

## 2026-10-29：catalog 资源选择对象观察

- 比较597条逻辑路径，Android APK/磁盘/空对象与 Linux 实际打开/缺失类别对应。
- Android update 清单变化，包装有效性、逐请求因果及 catalog 解析仍未验收。

## 2026-10-29：catalog 批量参数与资源路径

- 实测两侧5次批量调用参数一致，已观察597个 `csv_logic/` 请求路径集合一致。
- Android update为908项、Linux历史输入990项；解析与完整catalog仍未验收。

## 2026-10-29：catalog 差异槽对象字段对照

- 真实独立 Android/Linux 对照17槽的对象偏移字段，Linux 2,078项为 Android 子集。
- Android update 清单变化导致采集门失败，字段语义、根因与严格 catalog 仍未闭合。

## 2026-10-28：Android catalog 采集期间 update 清单变化

- 两次独立运行复现990变908，82项删除；严格清单门拒收，保留布局与失败证据。
- 删除项不在已观测 Linux loader 的131个 update 打开路径中，因果及调用来源未知。

## 2026-10-28：Android 与 Linux catalog 布局差异

- 隔离 Android 真实进程只读采集并与既有 Linux loader 对照；162槽相同、17槽行数不同。
- Android 25,810行、Linux 24,353行；运行阶段及本次 update 身份待对齐，根因未知。

## 2026-10-28：Android 与 Linux update 资源身份

- 比较 E1 历史与 Linux loader 的990项 update 清单及131个实际打开路径，SHA全部一致。
- 本结果限定文件身份；catalog 表值、Backup 与严格门仍未验收。

## 2026-10-28：历史 Android 与 Linux loader 的 APK 资源身份

- 匹配历史 E1 APK，逐项核验两 APK 的9,829资源字节及 Linux 619暂存资源清单。
- update、catalog表值与 Backup 平台等价仍未验收，严格门保持关闭。

## 2026-10-28：原生 Backup 队列中断恢复

- 增加同 inode、双链接发布中断的显式恢复，未知状态和活跃锁继续拒收。
- ARM64 Linux/Bionic 受控进程退出与恢复、合并请求、归档通过；非 Android Backup 等价。
- 专项988通过/1跳过；严格 catalog 门保持关闭。

## 2026-10-28：Android E1 与 Linux 资源局部对照

- SHA 绑定 Android 历史归档与真实 Linux libg 的24项文件路由及操作对照，独立复核通过。
- 补充证据篡改拒收测试；专项981通过/1跳过。完整 catalog 和 Backup 仍未验收。

## 2026-10-28：E2.1-N 真实 libg 资源路由矩阵

- 新增13例合成资源矩阵和SHA绑定独立验收，使用 ARM64 Linux 的真实 libg 与现有backend。
- 首轮12/13暴露无效文件仍调用length；修正probe后13/13通过，首轮结果保留。
- 有效文件覆盖读/长度/EOF/seek/释放，第三轮缺失例完成provider释放、dlclose及asset finish；不认定Android配对字节一致。
- 专项973通过/1跳过；严格catalog、Backup及后续战斗对象图仍未验收。

## 2026-10-28：E2.1-N 真实 catalog 资源轨迹审计

- 新增 SHA 绑定的资源轨迹审计器、边界测试及脱敏报告；重分析已运行的真实 libg。
- 247次APK open/read/close、131个update映射关闭；122个同名APK副本中118项SHA不同。
- 路径集合不证明请求因果，mmap不证明完整字节消费；Backup和严格catalog门仍未闭合。
- Linux专项964通过/1跳过；原始输入、资源及日志保持私有。

## 2026-10-28：E2.1-N 非空偏好编码键计数与真实 libg

- 修正非空 XML 用明文键统计导致的误判，绑定两槽 AES 编码键；依赖缺失时保持未知。
- 真实 ARM64 Linux libg 使用已有目标键的双槽副本，native exit0、条目数1/1；严格 wrapper exit1。
- loader ready与162表布局属局部观察；Backup等价、完整资源及catalog严格门仍未闭合。
- 专项957通过/1跳过；原始输入、私有日志和历史证据保留。

## 2026-10-28：E2.1-N 真实配置受控写入与恢复复核

- 在既有隔离 API 30 ARM64 AVD 中执行一次 `LOADING_SCREEN_STATE_LABEL` 写入并恢复原值；后续只读复核仍为原值。
- 目标与恢复各完成两个槽写入；4次同步 commit 均返回true，观察到完整 SharedPreferences 磁盘调用链。
- 两次 Backup 请求到达服务端数据变更和 job 调度；目标包原已pending，journal写入未观测，队列数保持30。
- 保留首次Frida attach失败；只启动AVD中既有Frida server。原始日志保持本机私有，AVD已关闭。
- 专项953/1、主工程211、公开回归1104/42、清单2583项及gitleaks均通过；偏好文件分阶段字节、跨进程恢复、Backup等价性和严格catalog仍未闭合。

## 2026-10-28：E2.1-N 当前材料绑定及真实事务字节对照

- 新增只读材料/Cipher捕获、局部Bionic重放、跨批次SHA审计及51项工具测试。
- 用户授权的一次store/恢复完成；当前o材料与历史不同，p及IV相同。
- 真实差异定位为空map序列化，修复后两进程8项XML字节一致，负证据保留。
- 专项951/1、主工程211、公开1104/42均exit0；253份原XML不变，首轮Python3.9错误保留。
- 真实libg三进程使用修复前probe，strict wrapper仍1；不将局部重放写成整体通过。
- Backup等价性及配置异常域仍BLOCKED，后续对象图保持NOT_RUN。

## 2026-10-28：E2.1-N 原生 Backup 候选绑定

- 新增私有目录持久入队、独立双槽归档消费者、限定证据审计及43项工程测试。
- 真实libg两个候选native0，自然loader到ready并解析162张表；默认对照native96。
- 三个strict wrapper仍exit1；Android Backup等价性与当前配置身份仍未闭合。
- 独立副本专项900/1、主工程211、公开1104/42均exit0，失败批次和历史证据保留。
- ResourceManager、GameState、Step及性能实验保持NOT_RUN，本轮没有启动AVD或更改VM配置。

## 2026-10-28：E2.1-N Backup 真实失败重排与重试

- 新增一次性IOException agent、采集runner、严格离线审计及47项独立测试。
- 实测一次请求后的失败重排和随后成功消费，73份命令归档校验通过。
- 保留exec-out丢远端退出码缺陷与首轮负例失败；v2修正仅通过工具回放，未重复真实store。
- 隔离专项857/1、主工程211、公开1104/42均exit0；专用实例已关闭。
- Linux Backup所有权及绑定、严格catalog仍BLOCKED；没有启动后续对象图、训练或性能验收。

## 2026-10-28：E2.1-N 非空 Backup 与真实队列消费

- 新增自有非空payload应用、独占runner、严格离线审计与37项工具测试。
- 分离直接备份和队列消费，记录transport/XML/state字节与journal消失观察。
- 56份命令归档通过离线审计；保留首次构建与安装路径审计失败。
- 隔离专项810/1、主工程211、公开1104/42均exit0，252份原XML字节未变。
- 专用实例关闭；Linux服务绑定与catalog仍BLOCKED，异常域及后续对象图未验收。

## 2026-10-28：E2.1-N 真实 Backup JobService 回调

- 新增四hook观察器、SHA/实例绑定runner、严格离线审计及48项工具测试。
- 自有应用一次请求与一次受控job执行，10事件且原方法返回；0bytes和worker覆盖缺口单列。
- 保留首次11hook准备期system_server崩溃，具体原因未知；补充四方法真实DEX静态边界。
- 独立副本专项773/1、主工程211、公开九组1104/42均exit0，251份原历史XML未变。
- 专用实例已关闭，Linux Backup绑定及严格catalog仍BLOCKED，后续对象图未启动。

## 2026-10-28：E2.1-N Backup 服务分支动态闭合

- 新增自有测试应用、八hook观察器、SHA/进程/AVD绑定runner和严格离线审计，36项新测试。
- 实测首次入队写journal、重复请求不写；独立平台JobScheduler提交被接受，交付保持UNKNOWN。
- 六次尝试、瞬时scope误判和旧解释器失败分别保留；最终专项725/1、主工程211、公开1104/42。
- 专用5582实例确认归属后关闭，原游戏实例/VM保留；Linux服务绑定与严格catalog仍BLOCKED。
- 本轮未加载libg或推进GameState，不把真实Android服务观察写成无VM引擎成功。

## 2026-10-28：E2.1-N 真实 Backup journal 跨环境局部对照

- 新增真实平台journal探针、UTF-16/modified UTF-8 C追加器、独占输出比较器及44项工具测试。
- Android真实框架与ARM64 Linux/Bionic两阶段均exit0；84/94bytes及各次追加字节一致。
- 实测rws写前/写后fsync，明确异常输入差别和断电/调度/服务绑定未覆盖。
- 保留首轮路径失败、v2退出码归档缺口；最终v3记录真实子进程退出码，不采用vmrun返回替代。
- 专项689通过/1跳过、主工程211通过；严格catalog仍BLOCKED，未加载libg或推进对象图。

## 2026-10-28：E2.1-N Backup 被动观察与 journal/scheduler 契约

- 新增只处理自然请求的同线程/对象身份探针及SHA、boot/starttime、六hook覆盖绑定runner。
- 真实45秒窗口NOT_OBSERVED；零事件不解释为依赖不存在，不重复store、Backup或重启。
- 新增六个精确DEX方法的人工审阅契约绑定，明确吞IOException及schedule去重/结果忽略。
- 新增43项工具边界测试通过；保留Linux副作用绑定和严格catalog的BLOCKED状态。

## 2026-10-28：E2.1-N Backup pending 去重契约

- 新增只读目标队列探针、独占0600 runner、精确方法SHA绑定的人工审阅契约及25项边界测试。
- 真实Android目标pending/participants存在，静态区分重复请求跳journal与正常schedule路径。
- 保留两次只读失败、metadata试验、身份拒收及初次fixture失败，历史动态分支保持未知。
- 隔离专项602通过/1跳过、主工程211通过；严格catalog仍BLOCKED，未推进后续对象图。

## 2026-10-28：E2.1-N loading 双槽磁盘事务

- 增加四阶段只读文件捕获、独占0600输出及SHA绑定离线审计，新增28项审计器测试。
- 真实Android一次目标写入与恢复，4次同步commit均true；双槽XML逐字节恢复，Backup顺序通过。
- 原始XML及trace留私有，历史失败与遗漏原样保留；跨进程因果归因保持未知。
- 独立副本专项577通过/1跳过、主工程211通过；严格catalog及Linux Backup绑定仍BLOCKED。
- 未推进ResourceManager、GameState、Step、正式训练或10×实验。

## 2026-10-28：E2.1-N loading 观察器作用域修复

- 修复匹配入口未打开作用域的漏记，嵌套与异常恢复父作用域；新增MemoryCommitResult对象身份关联。
- 观察器故障、关联上限与未归因写盘显式留痕，修复后的真实异步归因尚未运行。
- 事务入口默认只读，显式写入前捕获原值，并在目标失败时恢复；输出exclusive且逐条flush。
- 新增只读偏好路径工具，实测c/o/p文件映射及getKeyValue双槽读取链。
- 旧commit零计数更正为未覆盖，旧证据原字节保留；末态哈希相同不代表中途没有写盘。
- 专项549通过/1跳过、主工程211通过；严格catalog仍BLOCKED，后续对象图未启动。

## 2026-10-28：E2.1-N 受控 loading 配置事务

- 新增隔离 AVD 的一次真实写入/恢复 runner 与 Frida 观察扩展，记录文件对象和完整存储调用边。
- 观察到客户端 Backup 请求、Binder 服务调用、异步数据变更和 job 调度；保留 pending 队列原有成员事实。
- 事务最终值恢复为空；commit、journal、磁盘落盘、双槽及跨进程恢复仍未闭合，严格 catalog 门保持 BLOCKED。
- 全量 `tools/pure_linux` 回归 `531 passed, 1 skipped`，exit 0；未启动 ResourceManager、GameState、训练或性能实验。

## 2026-10-28：E2.1-N Backup callback 服务端静态路径

- 新增Android backup服务端DEX的SHA绑定、有界调用摘要器；11个真实方法只输出调用边和控制流类别，不输出指令。
- 新增摘要器边界单测3项；服务端摘要、ART trace与方法专项测试复验共49项通过，真实services DEX反汇编命令exit0、输入身份未变。
- 在新隔离目录重分析既有ART trace：23个requestBackup scope、2线程、正常返回且scope平衡；不代表本轮Android运行。
- 发现服务端静态权限/用户检查、handler、journal与KeyValueBackupJob路径；实际Binder交付及返回语义仍未观测。
- 严格catalog仍BLOCKED，ResourceManager BLOCKED，GameState/真实Step/完整局未运行；无训练或10×实验。

## 2026-10-28：E2.1-N双槽持久化及真实服务查询边界

- 新增 15 份源码/测试，修改 provider job 及测试；严格绑定材料/XML 身份，拒绝与旧候选混用。
- 独立 ARM64/Bionic 两 child 各 48 条历史 ASCII 参考一致，双槽重开、无关项及同值写入检查通过。
- 四个真实 libg child 完成双槽持久化后因 backup 未闭合主动 exit96，wrapper exit1，不放开 catalog。
- 两个真实 NDK checkService 查询在返回前 SIGABRT，/dev/binder ENOENT，不误报 SERVICE_ABSENT。
- 187 输入未变，材料内容及 SHA 不发布，保留 v115 collector 失败、全部原生失败及历史报告。
- 隔离专项 482 通过/1 跳过、主工程 211 通过、公开九组 1,104 通过/42 跳过；保留 248 份历史 XML。
- 当前身份、XML/异常全域及 backup 未闭合；GameState/真正 Step/完整局/no-VM/10×/训练未运行。

## 2026-10-28：E2.1-N配置文件原语与平台审计

- 新增8份源码/测试：文件原语、分步故障和进程恢复probe、双进程job及真实平台方法审计。
- ARM64/Bionic增强轮两个child各19组/444项通过，187输入未变；旧18组/402项记录保留。
- 离线Android30平台10方法STATIC_FOUND，恢复sync/bak错误处理及backup服务调用链。
- 保留提取拒收、UTF8边界初测失败；未将合成文件测试当作原版语义对照。
- 本轮未加载libg；当前身份、XML/map及backup未闭合，严格catalog/manager仍BLOCKED。
- 隔离专项409通过/1跳过、主工程211通过、公开九组1,104通过/42跳过，248份历史XML保持原字节。
- GameState、真正Step、完整局、no-VM、10×与训练均未运行；独立回归计数见本阶段机器证据。

## 2026-10-28：E2.1-N历史异常恢复及store0字节参考

- 新增原版result专用NCS1胶囊、持久上下文序列probe与双子进程严格核验；缺input显式拒收。
- 两份历史store1四步padding异常/恢复参考各两个Bionic子进程全匹配；XML字节差异明确保留。
- SHA绑定真实DEX与原版历史身份来源，store0两份参考各两个child、每child30条正常字节匹配。
- reference胶囊增加显式store选择，修复新测试发现的局部变量遮蔽；保留首次失败及后端差异。
- 新增8份源码和测试、修改2份工具；各187输入未变，未重载libg或启动Android/训练。
- 隔离专项378通过/1跳过、主工程211通过、公开九组1,104通过/42跳过；保留248份历史XML。
- 当前平台身份、storage/backup仍UNRESOLVED；严格catalog/manager BLOCKED，GameState/Step未运行。

## 2026-10-28：E2.1-N持久Cipher与独立原版字节参考

- 新增三持久EVP上下文、成功final后reset及错误状态保留，缺符号/未知材料显式失败。
- 新增实际映射路径核验，修复误将逻辑linker名称当文件；保留v97失败及v98诊断。
- 真实DEX提取p身份候选；从历史original字段独立构建30条正常路径参考，不使用native字段。
- 两个新ARM64/Bionic子进程各30条匹配、187输入未变；未重新加载libg或启动Android。
- 17份新源码和测试；专项304通过/1跳过，主工程211通过，公开九组零失败错误。
- 当前身份、异常全域、storage/backup未闭合；严格catalog/manager BLOCKED，GameState/Step未运行。

## 2026-10-28：E2.1-N配置控制流恢复与平台边界

- 恢复实际初始化分支/数组拓扑，有界图142节点/15分支相同，完整初始化差异保留。
- 新增单键显式后端C候选：双store分步commit、损坏清理、null/empty及backup异常顺序。
- 新增受限真实DEX解释差分，当前/历史各4,522例零差异，保留首次252例差异及修复原因。
- 两个ARM64/Bionic候选子进程exit0、187项输入未变；未加载libg，不冒充Android运行时等价。
- 补输入SHA及已有输出保护测试；保留密码、存储、backup和完整初始化未闭合状态。
- 严格catalog/manager门仍BLOCKED，GameState及真实Step未运行，无训练或10×结论。

## 2026-10-28：E2.1-N配置候选租约与初始化审计

- 增加进程生命周期flock、线程互斥和已初始化上下文fork拒绝；不冒充Android并发语义。
- 两次独立ARM64/Bionic租约/异常退出恢复实测，各11回执符合预期、187项输入未变。
- 增加SHA绑定的DEX方法审计，包含寄存器、异常表和数组字节；8个配置方法相同，初始化不同。
- 补齐配置语义差异矩阵与独立证据，181项专项通过/1条件跳过。
- 加密、commit/failure、初始化及backup契约未闭合，catalog严格门仍BLOCKED，GameState/Step未运行。

## 2026-10-28：E2.1-N catalog自然加载与配置归因纠正

- 真实两个ARM64子进程自然ready，53步、162表/24,353行，原始输入与辅助文件不变。
- 纠正旧SIGSEGV为配置写入桥获取JNIEnv，保留旧报告和失败原字节。
- 新增有界字符串、表布局与资源trace诊断、单键配置持久化候选及跨进程恢复测试。
- 真实update/APK同名资源读取通过；两次实际fallback路径有序关联，未知路径及资源fd残留0。
- 专项157通过/1条件跳过、主工程211通过、公开入口exit0且9组XML零失败错误。
- 配置Android语义未闭合，严格catalog/manager门仍BLOCKED，GameState及Step未运行。

## 2026-10-28：E2.1-N Provider 注册与 catalog 调查

- 新增版本绑定Provider/catalog探针、只读asset backend、所属子进程GDB观察与独立测试。
- data/update独立注册及释放通过，四次真实资源读取字节SHA、EOF/seek/close通过。
- catalog两次list/preload/分批调用返回，mmap资源证据补齐，ready仍0。
- 定位自然loader日志路径读取空JavaVM的SIGSEGV，保留失败；未跳日志或伪造VM。
- 108项专项通过/1条件跳过、主工程211项通过、公开9组XML零失败；各范围不累加。
- 新报告区分局部通过与catalog/manager阻塞，GameState/Step及完整局继续NOT_RUN。

## 2026-10-27：E2.1-N原生初始化对象图

- 新增真实加载/映射/content/filesystem探针、限额runner、constructor解析及GDB观测。
- libg753/753构造器实际返回；内容root/context和filesystem分配exit0，ready仍0。
- 限定post-load边界观测有正对照；零命中不冒充全路径无JNI/Asset依赖。
- 保留tagged-pointer、断点guard、REA超时及写盘失败证据，187项输入未变。
- pure_linux专项42项通过；catalog/manager门仍BLOCKED，GameState和Step未运行。
- 新增脱敏报告/JSON/回执，不修改main、冻结部署、旧报告和原始二进制。

## 2026-10-27：用户 APK 身份核验与真实 libg 加载

- 逐项核验新APK的8个ARM64库和9,829个游戏资产，匹配既有基线。
- 独立登记新输入，保留历史整包SHA差异、旧manifest和失败证据。
- 真实Ubuntu ARM64中执行原版libg的dlopen/dlclose，exit0，取得LOAD_ONLY。
- 保留linker警告；无新增shim，187个暂存输入前后SHA一致。
- 未验证战斗初始化、生命周期、no-JavaVM、Android-free、10×或训练。

## 2026-10-27 续验：真实 ARM64 guest 与 Bionic smoke

- 恢复经授权的现有 Ubuntu VM，真实 Linux 合成探针获得 PURE_LINUX_SMOKE_PASS。
- 修复自有 AArch64 raw clone TLS 参数顺序，保留初次 SIGSEGV 与 v4 成功记录。
- 匹配 Android linker/Bionic 合成 pthread/TLS 探针 exit0，核对实际 libc/libdl 绑定。
- 新增只读版本符号、relocation 写跨度、构造器槽审计及14项测试，专项总计29通过。
- 补充脱敏报告/回执/JSON及历史文档续验，不提交二进制、资源和原始审计。
- 原APK锁定身份仍缺失；真实libg加载、无VM生命周期、10×和训练均未运行。

## 2026-10-27：E2.1-N 原版 ELF 审计与 ARM64 Linux 探针

- 新增PT_DYNAMIC/PT_LOAD动态符号审计、manifest/SHA守门和ABI感知依赖候选发现。
- 新增direct ARM64 Linux syscall/线程/TLS/IPC/signal smoke、Bionic探针及排他输出runner。
- 15项专项通过，两种探针交叉构建成功；真实Darwin守门exit2，未执行Linux ELF。
- 核验214个运行文件和990个资源；185个ARM64库仅为静态候选，原APK身份仍缺失。
- 补齐依赖图、ABI报告、阶段方案和回执，保留旧草稿/失败；纠正tick读取与推进区别。
- 无可用live guest，状态BLOCKED_ENVIRONMENT；原版加载/生命周期、无VM、10×和训练未运行。

## 2026-10-25：整轮顺序执行与独立回收恢复

- 固定准备结果、选优报告及执行身份，逐局运行既有worker和独立归档验收器。
- 成功任务恢复后重验不重采集；存活、未知或失败worker停止并保留现场。
- 准备文件/输出排他锁及主机/boot_id绑定，整轮提交前重验所有接收树。
- 初版29项、最终联合117项及主工程211项通过，范围重叠不累加。
- 完整公开入口通过，新增协调65项零跳过；2329项清单通过，凭据扫描零发现。
- 尚无真实round2准入，重试调度和训练交接待完成；G0/G1与34/43保持开放。

## 2026-10-25：整轮补充采集协调预检

- 接入已有全历史/外部种子来源、部署与请求校验，要求当前轮任务精确覆盖。
- 隔离子进程预检全部成功后才发布准备标记与既有worker命令；不启动引擎。
- 核对完整父数据树及冻结输入，失败保留现场；32项专项与128项联合回归通过。
- fixture不替代真实模型和原生round2；整轮执行/回收、重试与梯度恢复仍待验。
- 补齐闭合清单的真实坐标限定证据；G0/G1及34/43保持开放，不放量。
- 保留首次公开32项初始化错误报告；共享fixture改用显式合成manifest，真实历史审计不变。
- 修复后源联合113项、公开新专项32项通过且零跳过，完整公开入口通过；主工程211项通过。

## 2026-10-25：预约坐标到原生加载字段限定验收

- 独立v5插件新增X2读取，固定执行器加载与helper保存指令，不改冻结部署。
- 62份归档独立重算，三组完整终局一致；188预约坐标逐条一致，其中33个带位置偏移。
- 41项最终专项通过，含真实归档重验、旧v4拒收及3,456种冻结转换对照。
- 保留首次测试误用类名失败；转换对照隔离进程并核对冻结导入路径，公开新专项独立运行。
- 内部卡牌、完整mask、一般拒绝与全部随机覆盖仍开放，G0/G1及34/43不增。
- Full128提交183组/366根/46848模拟继续，terminal第18步验证中，未启动放量。

## 2026-10-25：逐预约原生对象映射限定验收

- 独立记录同步入队日志范围，按真实对象关联预约、构造、执行、回执与最终结算。
- 三独立进程完整轨迹一致，62份归档实际重算通过，188预约全部覆盖，所属清理通过。
- 覆盖68个双方同时执行tick及20个对象跨tick复用；最终33项专项通过含真实归档重验。
- 公开检查独立运行映射专项；原始地址/寄存器/回执不公开，缺私有附件显式跳过。
- 内部动作字段、坐标转换、一般拒绝及全随机覆盖仍待验；G0/G1与34/43保持开放。
- Full128提交182组/364根/46592模拟继续，terminal第18步验证中，不改冻结部署或放量。

## 2026-10-25：虚分派实际目标完整轨迹限定验收

- 独立v4日志新增X8字段及调用前/返回后两点，保留父部署与历史v3证据。
- 三独立进程各426次决策、20个观察边界和tick5897终局一致，62份归档独立重算通过。
- 188次实际目标、对象、参数、LR和返回值完整配对；所属引擎及端口清理通过。
- 最终34项专项通过，含真实归档重验及旧v3真实成功归档缺X8时拒收；公开新专项独立运行。
- 仅本轨迹虚分派调用点已证明；逐预约映射、一般拒绝、完整mask及随机全覆盖仍待验。
- Full128提交180组/360根/46080模拟继续运行；G0/G1与34/43保持，不改冻结部署或启动放量。

## 2026-10-25：原生动作执行链完整轨迹限定验收

- 在独立只读插件新增七处指令观察点，固定库与指令身份，保留八处随机入口正向对照。
- 两次启动失败独立拒收并保留；过滤ARM前新增点后第三次三组完整对局一致、所属清理通过。
- 188次ct86执行器与位置helper调用完整配对，工厂/构造与对象关联齐全；返回码均0。
- 最终29项专项通过，含真实完整归档重算；公开检查隔离十五点与父八点进程内状态。
- 不证明一般拒绝可达性、虚分派目标寄存器、逐预约对象映射或完整mask；G0/G1及34/43不变。
- Full128提交179组、tick5874继续，terminal恢复到第17步；不修改后台冻结部署或启动放量。

## 2026-10-25：位置检查原始指令条件化核验

- 独立CPU执行81条原始AArch64指令，14,676个确定性样例一致，指令与返回码全部覆盖。
- 增加除500的全signed32整数证书、ct86拒绝码映射与调用约定核验，18项专项通过。
- 原始游戏库不公开，缺依赖或私有库的CPU测试显式跳过；新专项采用独立进程。
- 合成结构体不证明游戏可达性、terrain函数体或完整mask充分性，G0/G1及34/43不变。
- Full128提交177组、tick5864继续，terminal仍运行；不改冻结部署或启动放量。

## 2026-10-25：最早合法延迟原生诊断

- 补充旧delay1/30探针：从真实mask选择最早合法delay及下一档，双方视图先冻结再提交。
- 四根37组/74次续演实际独立验收通过，合法拒绝与单方就绪均0组，快照和所属引擎清理齐全。
- 新增严格身份/完整候选序列/语义篡改验证，23项专项通过，公开检查以独立进程运行。
- 有限负结果不扩为全局不可达；G0/G1及34/43保持开放，不修改后台冻结部署或启动放量。
- Full128快照提交176组、tick5804继续；真实terminal与完整局仍待验，原始私有归档不公开。

## 2026-10-25：全局快照原生搜索限定收尾与恢复交接

- 四根原生Full128独立验收通过，共512有效模拟与两对隐藏干预，369份归档和清理齐全。
- 保留首次验收CLI包版本冲突的拒收记录；新增隔离入口，冻结验收器与部署原样保持。
- 新增terminal独立retry到补充采集包的准备/复核，不伪造旧格式，不自动开始下一轮。
- 联合82项工程测试通过，含39项交接；写出SHA复核、活动父拒收与失败隔离齐全。
- Full128同局继续至175组，真实terminal仍待验；G0/G1及34/43保持开放，未五机放量。
- 发布复核：公开794通过/27跳过，独立恢复与交接82通过、主工程211通过，2244项清单及凭据扫描通过。

## 2026-10-25：terminal超时独立恢复

- 新增父超时内容重验、独立24小时续跑、稳定收取和terminal内容验收工具，保留父失败。
- 43项联合工程测试通过，含17项新增；小模型完整状态恢复一致，原始失败报告保留。
- .35 GPU1实际从第15步启动续跑，原生terminal仍待验；不自动启动round2。
- Full128同局恢复174组至tick5728并继续，全局MT搜索完成首根；G0/G1及34/43不变。
- 私有游标、权重和原始审计不公开；公开回归独立进程避免冻结版本模块缓存串用。

## 2026-10-24：剩余MT初始化参数的局部静态审计

- 实际重算18处直接引用的X0对象/W1种子，分类为11栈相对、6分配器返回、1传入。
- 明确X/W别名、调用失效寄存器、未知写入与片段内部控制转移；核对operator new导入。
- 25项专项、联合130项通过；保留首次两项失败原字节及修正说明。
- 不把局部参数分类当作生命周期/可达性/快照覆盖证明；五机失联时保持冻结部署及门状态。
- 公开794通过/27项缺私有附件跳过；2220项源码清单通过，凭据扫描零发现。

## 2026-10-24：全局随机快照搜索集成与逐根审计

- 新增独立准备/派发/收取/验收入口，严格host key、父证据重验、353项载荷身份及所属进程清理。
- 逐次核对完整native+global-MT摘要与恢复身份，逐根绑定日志区间，拒绝缺失、重叠及伪总数。
- 38项专项、联合139项通过；原生新搜索待运行，不将本地工程检查登记为G0/G1通过。
- 修正闭合清单中尚未索引的插件限定成功；五机失联时保留当前状态未知及所有冻结部署。
- 公开770通过/26项缺私有附件跳过，主工程211通过，2212项清单通过，凭据扫描零发现。

## 2026-10-23：独立插件测量环境与精确入口回调

- 独立构建QEMU10.0.13 plugins，固定源码/依赖/产物身份，仅解包依赖、四线程，不改冻结部署。
- 修复vCPU槽复用与块内PC误判，分别保存回调地址和原始寄存器；真实入口/块内合成指令检查通过。
- v3在reset前绑定本次进程实际库地址；完整原生终局后监督封存，不伪造END或接受缺正向对照。
- 三组原生完整轨迹通过限定内容验收：426条决策/20个观察边界一致，44条已知入口记录及正向对照齐全。
- 五次原生失败保留；不升级全随机覆盖门。联合139通过、公开734通过/24跳过、主工程211通过。
- 2202项源码清单通过，凭据扫描零发现；历史失败XML仅逐路径保留原始尾随空白。
- Full128继续到133组，G0/G1与34/43保持；原始审计及第三方产物不公开。

## 2026-10-23：随机入口追踪工程及失败证据

- 新增独立校准/追踪入口、冻结身份、有界收取、原始日志及完整预约/终局验收。
- 拒收未执行TB、日志交错、缺正向对照与归档篡改；61项专项通过，实际失败归档拒收。
- 保留四次失败及SIGALRM启动阻断；未取得真实完整执行证据，不关闭随机覆盖门。
- Full128继续至127组提交；冻结部署、G0/G1与34/43保持原状态。
- 公开回归663通过/17项缺私有附件跳过，主工程211通过、联合163通过，凭据扫描零发现。

## 2026-10-23：terminal选优到采集包交接

- 新增强制独立内容验收的selection构建模式，继承冻结训练实现及确定性运行时。
- 绑定父execution/completion/checkpoint身份，拒收验收后篡改及不完整、含混参数。
- 35项专项、联合78项通过；真实terminal仍运行，真实交接和round2尚未执行。
- Full128保持运行及冻结身份；G0/G1与34/43不变，不升级正式迁移或训练准入。
- 公开回归603通过/16项缺私有库跳过，主工程211通过，凭据扫描零发现。

## 2026-10-23：原生开局先验诊断完成

- 1024个预注册seed主采样、第二新进程64个seed复测及所属引擎清理独立验收通过。
- 21项预注册检验未发现显著偏离，公开统计与身份摘要，原始牌序/seed审计私有保留。
- 明确有限诊断边界，不宣称精确均匀或历史后验校准；G0/G1与34/43不变。
- 公开回归584通过/16项缺私有库跳过，主工程211通过，凭据扫描零发现。

## 2026-10-23：单方就绪边界的条件分析

- 新增真实运行时1,981组合时序对照及带源码身份的矩阵摘要；961种全成功组合保持同步。
- 明确较早私有拒绝导致单方就绪的条件反例；合成结果不代替合法原生可达性证据。
- 11项专项、联合45项通过，保留首次命令路径错误；G0/G1及冻结部署状态不变。

## 2026-10-23：有战斗的预约快照续演限定验收

- 双独立新进程六个pending根各两条60tick战斗分支一致，完整根与全局MT恢复通过。
- 每进程ACT执行14/13次、正常取消5次，公开实体/血损证据齐全，21份归档及所属引擎清理独立核验。
- 新增55项专项，联合240项通过；整包篡改重算回执仍拒收，公开实现/摘要并保留私有原始归档。
- 全局MT只读未见变化，不外推随机全覆盖；Full128冻结部署保持，G0/G1及34/43不变。

## 2026-10-23：独立全局MT快照与摘要覆盖修复

- 新版保存/恢复2504字节全局MT，绑定库及执行身份，拒绝多slot/render和损坏快照。
- 新摘要显式覆盖native payload及全局MT，保留nativeSha256，旧构建与冻结任务原样保留。
- 双新进程8边界完整恢复及重复WAIT续演通过，19份归档独立验收；新47项、联合185项工程测试通过。
- 固定速猪实际消费者、战斗续演与全部随机覆盖仍待验，G0/G1和训练准入未升级。

## 2026-10-23：全局随机状态快照缺口原生诊断

- 补齐全局MT加载初始化注册入口及三段消费者字节，固定速猪实际使用仍待验。
- 独立双进程8个边界均证实受控扰动未被快照恢复、摘要未检测；零推进、CAS修复及引擎清理通过。
- 新33项、随机机制联合138项通过；新增整包篡改拒收测试并纳入公开检查。
- 原始内存审计保持私有，冻结部署和后台Full128不变；不升级G0/G1或实战影响结论。

## 2026-10-23：随机初始化跳转入口补充审计

- 反向追踪无条件跳转入口，18处直接初始化引用扩展为19条闭包边，保留完整调用图缺口。
- 定位额外time seed与全局对象，绑定固定字节及time导入；实际使用和快照覆盖仍待证。
- 新增31项，与种子/洗牌联合105项通过；纳入公开检查，不修改运行部署或升级G0/G1。

## 2026-10-23：恢复重试独立收尾与开局先验诊断

- active重试完成第5→7步并通过双归档内容验收，父超时失败保留；未重验原始对局来源，不升级G2或训练准入。
- 修复派发公开目录误判，兼容源/导出布局及.git文件，32项专项通过；真实terminal选优独立启动，结果待验。
- 新增1024seed开局诊断与64seed跨进程复测、21个事前多重校正检验、独立归档验收器；34项工程测试通过，原生采样进行中。
- 将新增诊断与派发测试纳入公开检查，保留首采样失败及冻结部署；G0/G1与34/43仍开放。

## 2026-10-23：手牌临时随机路径静态补证

- 独立审计14段固定库摘要与18处MT初始化直接引用的唯一函数归属，保留16处未解释引用。
- 绑定手牌seed来源及栈临时对象，正常控制流所有返回前清理；将ct1548/subtype55路径另行定位，不宣称固定卡组不可达。
- 新专项24项，与seed生命周期联合74项通过、零跳过；静态/工程检查不替代原生路径覆盖、先验校准或完整搜索终局。
- Full128及active恢复后台继续，冻结部署未变；G0/G1与34/43保持开放，未开始五机放量。

## 2026-10-23：原生种子生命周期限定补证

- 新增固定库种子语义、构造器注册、time导入及只读进程审计，独立双进程四阶段原生验证通过。
- 同seed跨进程和同进程重复reset一致；快照重复续演、过期身份拒收及所属引擎清理通过。
- 保留首个探针失败归档，新探针换连接后重跑；50项本机测试通过，公开缺私有库时对应检查跳过。
- 缓存本次在首局前已填充，未原生覆盖增量1及全部随机机制；后台Full128与恢复继续，G0/G1开放。

## 2026-10-23：组合provider原生local限定验收完成

- 四根共512有效模拟独立内容验收通过，tick110/142两组隐藏配对结果一致；343项源码与33份归档核验，所属双引擎清理完成。
- 公开逐根耗时、增量锚点及证据SHA，不从四根外推长局性能；原生fallback、pending、先验校准与完整终局仍待验。
- 新增ct86保存参数寿命静态审计和15项测试：参数2/3的显式bit1读取相同；不证明opaque helper、整个执行器或两条队列等价，不证明合法拒绝不可达。
- 保持后台Full128及冻结部署；G0/G1开放、34/43限定登记不变，未进入G2或五机正式放量。

## 2026-10-23：原生队列mode只读证据

- 绑定27条固定libg指令，独立原生WAIT轨迹四阶段均为world_type3、bit0-set；不将age20静态分支直接当作当前原生执行路径。
- 只读身份校验、快照重复续演、独立归档/验收及引擎清理通过，32项工程/篡改测试通过；G0/G1仍开放。
- 中心最新只读可用206.93GiB，超过200GiB瞬时底线，两代实际容量与正式链路待验；本轮未清历史数据。

## 2026-10-23：一般provider原生local路径验收

- 新增独立双引擎部署、原生探针适配和内容验收器，不修改组合provider或冻结history-v2源码。
- 覆盖tick110及后继根的Full128、隐藏配对、独立构造和清理；原生任务已启动，结果待验，不新增总门PASS。
- 工程/篡改测试使用独立synthetic fixture；公开回归纳入，保留原生pending/fallback与校准缺口。

## 2026-10-23：Full128同局24小时续跑

- 显式冻结源码恢复入口：父段244项归档源码逐文件核验，先选择模块再导入，修复开发源码更新导致旧搜索协议身份误拒。
- 新段支持86400秒，保留1800秒完整边界保存余量；启动、worker、探针和验收同步，兼容历史21600秒段。
- 51项专项通过9.16秒；真实父链103组/206根/26368模拟核验通过，.35同局续跑段已启动，终局待验。
- 如实记录active父trainable恢复超总时限exit124，保留失败现场；G0/G1及中心200GiB门继续开放。

## 2026-10-23：远程选优派发与回收工程原型

- fetch全部远端分支确认最新主线`5c54e0478747d30be2472f855c06a871d8866406`；更新前2015项源码清单与源工作区逐项匹配。
- 增加安全SSH/SFTP派发、目标机GPU/内存/磁盘预检、GPU lease、完整上传树核验及远端元数据SHA/字节数/内容交叉验证；重复回收重新验收。
- 全模拟远端launch/回收专项29个测试案例通过；与selection交接、执行、active verifier联合93项通过98.774秒，测试集合有重叠，不累加。
- 未连接真实GPU主机或启动选优；不改冻结部署、不新增G0/G1通过项，父恢复、真实交接、terminal选优、G2–G8和模型强度状态保持。

## 2026-10-22：最新主线独立预算选优与终止内容验收

- fetch全部远端分支并核对最新8f68763，本地与origin/main一致；2010项旧源清单通过。
- 新worker只执行交接中的原字节冻结部署，保留完整第7步模型/AdamW/RNG/游标/历史，使用独立有界预算、进程组清理及失败隔离。
- 独立内容验收绑定执行/完成文件SHA、数据/父身份，重验terminal实际checkpoint和报告；修复12轮结束采样器多洗牌及Tensor状态比较。
- 联合64项通过184.31秒，含26项新工具测试、17项交接及21项父验收测试，不累加；小模型与子进程fixture不作原生验收。
- 发布复验：公开参考220项、主工程CPU 211项通过；2015项公开源清单通过，凭据扫描无发现，各集合分别记录。
- active完成4次冻结更新，Full128提交96组/192根；真实父恢复、交接、终止选优仍待验，整体门与正式训练状态保持。

## 2026-10-22：最新主线选优交接准备工具

- 基于GitHub最新f9e0958继续，更新前2005项源清单通过；运行中的冻结部署保持。
- 新工具先独立验收新版原生恢复，再复制原字节载荷和完整第7步checkpoint，绑定父/实现/数据身份；仅准备、不执行选优。
- 专项17项、联合38项通过，范围重叠不累加；冻结参考220项、主工程211项分别通过。
- 旧batch选优总预算耗尽退出124，保留失败现场；active恢复及Full128完整终局仍待验，不新增门PASS或启动正式训练。

## 2026-10-22：最新主线双阶段恢复归档独立验收工具

- 再次fetch全部远端分支，确认da8ca6c为最新main；源清单2000项与工作区逐项一致。
- 新验收器绑定外部launch/回执SHA、部署与数据，重算六checkpoint的模型/AdamW/RNG/采样/选优内容，校验encoder冻结/解冻及真实256/96批次日程。
- 专项21项、载荷/恢复/验收联合31项通过，范围重叠不累加；拒收重封装后篡改、失败/缺失阶段、重复进程及缺失附件。
- 新版原生reference已完成256行和96行两次更新，两个新进程恢复仍待运行；Full128已提交94组/188根、尚未终局。整体门和正式训练状态保持，冻结运行部署未修改。

## 2026-10-22：最新主线独立验证发现并修复解冻KL数值回归

- 重新fetch全部远端分支，确认af498d1为最新main；新增独立277文件载荷，绑定active六文件和保留旧scalar参考。
- 新增10项载荷/恢复CPU测试通过；恢复身份纳入active源码，归档检查另绑定上传包SHA。
- 首次CUDA尝试解冻阶段AdamW更新超容差，失败归档287文件保留；修复教师侧仍合批的路径，教师/学生解冻均逐位置，冻结保留合批。learning 11项、migration 26项通过，验收容差不变。
- 修复后.35空闲GPU1六对CUDA损失/梯度/AdamW比较通过；有界6小时链路已进入reference，冻结/解冻新进程恢复及最终独立归档核验仍待完成。
- 保留Full128完整局与旧选优任务，G0/G1与正式迁移门保持原状态。

## 2026-10-22：同步远端最新审计实现并继续工程验收

- 核对全部远端分支，fast-forward整合最新8195a5a及其5个提交；保留本机归档验收器成果与备份。
- 新源码8文件逐项核对旧基线后同步回源工程，重新生成来源清单，避免导出时回退新版代码。
- 当前主工程211项、冻结参考版220项CPU测试分别通过，旧原生恢复证据不升级为新实现PASS。
- 旧归档及batch-v1在新active源码下的兼容性检查75项通过、3项因私有附件缺失跳过；源码清单1,990项通过，凭据扫描零发现。
- 更新交接回执与后续验证边界；原生运行部署和整体门状态保持。

## 2026-10-22：当前主工程CI、KL、PUCT和迁移继承审计（分支待验）

- 在独立分支新增当前主工程CPU测试入口和CI，不替换已有冻结历史版本检查。
- 为迁移条件KL提供Delay Head合批；按既有CUDA工程对照要求保留解冻Encoder时的旧反向求导路径和逐位置KL累计。
- PUCT首次访问以先验选择，旧字典序规则保留为同128预算消融；训练/评测根噪声模式显式锁定。
- 新增Encoder精确复制覆盖率与未复制Head统计，交接文档写明实际权重迁移与batch256/确认集验证边界。
- **待验收**：当前主工程CI状态与原生五阶段迁移验证。冻结运行部署、现任Champion和正式Gate状态均不改变。

## 2026-10-22：后续采集归档独立内容验收

- 新验收器固定输入SHA、只读本地映射，重验终止选优父、完整整局及原始终局、种子来源、所属引擎身份和清理。
- 核验确定性运行时源码及声明，明确未重跑CUDA或教师标签；联合47项通过65.794秒，fixture不替代原生采集。
- 修复多轮只读数据身份与父optimizer步偏移核验，支持真实补充轮身份对照。
- Full128同一局保存至86组后恢复继续；新合批链路进入选优。整体门和正式训练状态保持。

## 2026-10-22：原生确定性跨进程恢复独立验收

- 三局1,044行工程数据、FP32 batch256累计三次更新，连续与新进程恢复后的模型、AdamW、RNG、采样游标及选优历史严格一致。
- 外部固定父/恢复launch与归档回执SHA，重新核验冻结部署、不同进程身份和原生来源；公开内容验收摘要，私有附件保留本地。
- 新后续采集worker补齐预检、独占引擎身份、超时和退出清理，运行中的冻结部署保持原样；尚未据此宣称原生补充采集通过。
- G0/G1仍开放，34/43登记保持，正式迁移和模型强度验收未运行。

## 2026-10-22：采集载荷确定性身份交接

- 为缺旧声明的合批包加入显式固定祖先身份核验，默认严格拒收保持；未修改运行部署。
- 实际采集包276源码SHA复核，273父文件保持；24项联合测试通过，尚未部署或采集原生round2。

## 2026-10-22：嵌套历史评测种子审计

- 只读回收四组16份历史来源，审计3,200条结果及任务、模型、执行代码记录身份。
- 新增1,600个评测排除seed，总集合7,160个；训练/价值清单原字节保持，三类交叉重叠为空。
- 专项54项、联合190项通过，范围不相加；5条结局不明确记录不升级真实平局，全部记录仅作种子排除。
- 公开实现、XML和SHA摘要，私有原始附件外置；历史普查仍有缺口，G0/G1与正式迁移门状态保持。

## 2026-10-22：补全新增文件发布检查

- 发布前先显式暂存，再检查暂存差异；检查失败时停止提交，覆盖新文件。
- 为历史失败XML精确登记行尾空白属性，保留原始回溯与SHA，不批量豁免源码。

## 2026-10-22：迁移 KL 数值一致性修复与原生对照

- 保留完整合法支持；缓存教师条件化，冻结encoder时合批学生延迟头，解冻时保留原学生反向图。
- 修复AdamW放大微小梯度差导致的参数容差失败，原阈值保持，失败现场保留。
- 六对真实CUDA对照通过，274个载荷文件及三局数据身份独立复核；31项分派与16项恢复/验收器测试分别通过。
- 新五阶段工程链路进入训练，原生完整恢复、选优和Full128终局尚待完成；G0/G1仍开放，非正式G2。

## 2026-10-22：确认仓库名称 Nashura

- 核实公开仓库名称为 Nashura，远端地址为 https://github.com/Coder-Osman/Nashura 。
- 同步工程维护规则中的 GitHub 账号和仓库引用。

## 2026-10-22：首次公开工程快照

- 汇集主工程、冻结规范、独立版本、测试与阶段证据。
- 添加成果索引、公开范围、复现说明、阶段同步规则和来源 SHA 清单。
- 公开副本移除历史演示脚本的内嵌凭据默认值。
- G0/G1 保持进行中，G2–G8 未运行；限定子项 34/43 的统计口径保持。
- 本次发布验证结果见 publication/validation.json。
## 2026-10-23：独立恢复重试与选优证据链

- 为共享预算超时的active父增加独立解冻恢复；父保留失败，冻结输入/实现和日程保持。
- 新双归档验收重核六checkpoint、新进程、命令与确定性环境；原生重试仍在运行。
- 补齐显式失败父→成功重试→选优交接，拒收缺链、篡改、身份复用及正式门升级。
- Full128后台继续，107组/214根/27392有效模拟；未终局，G0/G1与34/43保持开放。
