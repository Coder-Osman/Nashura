# Codex 执行规范：原版战斗引擎脱离 Android，在 Linux 直接运行

- 日期：2026-10-26
- 仓库：Coder-Osman/Nashura
- 工作分支：codex/pure-linux-libg-headless-20261026
- 目标：普通 Linux x86_64 主机直接驱动原版 ARM64 战斗逻辑；不启动 Android Emulator、APK Activity、Android UI、ART 或 Java VM。
- 性能门槛：同等硬件、同等动作轨迹和终局语义下，吞吐至少达到当前 Android 模拟器路径的 10 倍。此为待验证目标，不得预先宣称达成。
- 本文不授权启动正式训练，不允许修改 main 或干扰冻结任务。

## 1. 完成定义

只有同时满足以下条件，才可报告 PURE_LINUX_NATIVE_ENGINE=PASS：

1. 实际运行匹配版本 libg.so 中的战斗逻辑，不能把自写模拟器说成原版引擎。
2. 在普通 Linux x86_64 用户进程中运行。允许 QEMU user-mode 翻译 ARM64 指令；它不是 Android Emulator，也不需要 KVM，但不会自动提供 Android ABI。
3. 不启动 Android Emulator、APK Activity、Android framework、ART、Java VM、Surface/EGL/GLES 或 Android UI。
4. 不通过截图、ADB touch、Frida 附加 Android 进程或读取 Android 进程内存来控制对局。
5. 训练程序通过本机 IPC 发送动作并获取结构化状态。
6. 能创建新局、初始化资源、接受合法动作、拒绝非法动作、推进 tick、读取状态、重置并自然到达原生终局。
7. 通过真实原生轨迹对照与可重复吞吐基准。动态库加载、单函数调用、静态分析或合成 AArch64 程序成功均不算通过。

## 2. 现有仓库的真实状态

### 2.1 原生路径仍依赖 Android

- firstlight_CR/native_runner/cr_native_env.py：Python 通过 TCP JSON 控制口（默认 127.0.0.1:26789）控制真实 ARM64 引擎；headless 指在隔离 stock client 内运行，不代表脱离 Android。
- firstlight_CR/native_runner/battle_env.py：模型侧观察、动作、mask、事件与终局适配，应尽量保留。
- firstlight_CR/native_runner/offline_build.py：修改 APK smali 加载 libcrprobe.so，依赖客户端初始化。
- firstlight_CR/artifacts/standalone-resident-20260927/serve_jni.c：入口是 JNI 方法 Java_AssetProbe_serve，调用 GetJavaVM 后进入 resident。
- 同目录 resident_export.inc：cr_standalone_serve 明确要求 JavaVM 指针，并调用 find_libg_base 与 inspect_content_runtime。
- 同目录 frozen/assetprobe.c：调用 AAssetManager_fromJava、GetJavaVM、libandroid.so 和 APK asset API，将真实 AssetManager 绑定给游戏引擎。
- 同目录 frozen/android_reference_probe.cpp 与 frozen/probe/cr_replay_probe.cpp：包含 jni.h、android/log.h、JavaVM、Android 属性/运行时路径及版本绑定偏移。这些是原生逆向与状态对照资产，但仍处于 Android 进程上下文。
- firstlight_CR/native_runner/sim_core/：纯 Python headless 原型，但 ruleset.py 只支持速猪 2.6 的 8 张卡。它是独立模拟器，不是原版完整引擎。

结论：现有代码有“真实引擎 + Android 客户端内的 headless 控制”和“纯 Python 8 卡模拟器”两条路径；尚不能把前者称为纯 Linux 原生执行。

### 2.2 外部仓库的作用

Jason-XII/clash-royale-simulator 是自写模拟器。README 声称 M4 上约 0.26 秒模拟完 180 秒对局、约 658 倍实时，并实现 47 张卡。可借鉴 A* 寻路、静态 walkability/tile cost、建筑布局变化时的路径缓存、无渲染模式、Gymnasium 接口和 benchmark；它不是 libg.so 的 Linux 移植，吞吐数字不能直接与 Nashura 原生引擎比较。

参考：
- https://github.com/Jason-XII/clash-royale-simulator/blob/main/src/clasher_new/battle.py
- https://github.com/Jason-XII/clash-royale-simulator/blob/main/src/clasher_new/pathfinding_heap.py
- https://github.com/Jason-XII/clash-royale-simulator/blob/main/src/clasher_new/environment.py
- https://github.com/Jason-XII/clash-royale-simulator/blob/main/src/clasher_new/benchmark_speed.py

Jason-XII/cr-memory-reader 是 Android 游戏进程状态读取器，可帮助理解圣水、时间、手牌、塔血量和实体字段，提供真实轨迹对照；它仍依赖 Android 进程和内存读取，不能替代战斗 tick 执行。
参考：https://github.com/Jason-XII/cr-memory-reader/blob/main/README.md

## 3. 技术障碍

libg.so 是 ARM64 Android native library，不是普通 x86_64 Linux ELF。复制到 Linux 后直接 dlopen 不够，必须逐项解决：

1. ARM64 指令执行：QEMU user-mode 可翻译 ARM64 Linux-user 指令，不需要 KVM，但不提供 Android ABI。
2. 动态链接与 libc：Android ELF 使用 Bionic/Android ABI，普通 glibc 环境不等价。
3. JNI/JavaVM：初始化可能调用 JNI 回调、线程 attach、字符串/数组、全局引用和异常状态。
4. 资源：现有 assetprobe 通过 AAssetManager_fromJava 绑定 APK 资源。纯 Linux 需要语义等价的资源适配，或证明可绕过这条路径。
5. Android API：属性、日志、线程、时钟、文件路径、动态加载、TLS 和同步原语。
6. 初始化对象图：tick/step 需要有效上下文、数据表、战斗管理器、状态和资源对象。
7. 生命周期：必须从无 UI 的状态创建战斗、推进、取状态、结束和销毁。
8. 隔离：每局要有独立随机状态、对象池、事件缓存和终局状态。先实现每进程一局，再考虑并行。

## 4. 推荐路线

### A：QEMU user-mode + 最小 ABI / 资源适配

目标结构：训练程序 / BattleEnvV1 → Unix domain socket → Linux x86_64 host → QEMU AArch64 user-mode worker → libg.so + 最小必要 ABI shim → 原生战斗状态 / tick / 动作 / 观测。

QEMU 只负责指令翻译，不会自动解决 Bionic、JNI、AssetManager 或游戏初始化。先查依赖，再对真正可达且必需的函数逐个实现 shim。每个 shim 都要有契约测试，不允许对未知函数统一返回 0 或 null。如果 worker 启动完整 ART/Java VM 或 Android system_server，则不符合本文定义。若使用局部 Android userspace/Bionic 组件，必须如实记录为 Android ABI 兼容层。

### B：抽取原版战斗更新路径

若 Android/JNI 依赖主要集中在启动和资源加载，而每 tick 的战斗更新依赖较少，识别：内容资源加载和全局注册；GameState 构造、初始化、载入；BattleManager / ReplayBattleController 创建和生命周期；单 tick/update；原生状态快照；动作构造、校验、排队和应用时刻；正常销毁与 reset。

优先复用原版状态创建、动作处理和更新函数，不要自己重写移动、寻敌、伤害、弹道和卡牌行为。frozen/android_reference_probe.cpp 中 GameState new/constructor/setup/step/tick/destroy、JSON/logic command、ReplayBattleController 偏移只是匹配版本的候选入口。每次使用前核对 libg.so SHA、机器码签名、调用约定、前置对象和返回语义，不能跨版本复制偏移。

如果每 tick 都依赖 Java 对象、Android Looper、图形上下文或不可分离的客户端全局状态，量化依赖闭包，不要盲目添加 stub。

### C：自有高保真模拟器（后备）

复用 firstlight_CR/native_runner/sim_core 的接口与测试，或借鉴外部 simulator 的 A* 与机制模块。此路线可能达到更高吞吐，但属于重新实现，不得声称原版引擎已剥离或 122 卡等价。必须按卡牌/机制建立原生对照与 sim-to-real 误差报告。

## 5. Codex 分阶段执行

### Phase 0：保护现状并锁定输入

记录 git status --short --branch、HEAD SHA、origin/main SHA。重读 AGENTS.md、README.md、CHANGELOG.md、TRAINING_ENGINE_PLAN.md、docs/REPRODUCING.md、supported_engine.json 与 data/competitive/manifest.json。

核对 APK 版本、ABI、libg.so SHA、runtime fingerprint 和资源身份。缺少匹配输入则报告 BLOCKED_MISSING_INPUT，不得用其他版本代替。检查运行中的冻结任务与输出目录；不得发信号、重启、覆盖文件或复用端口。

输出 agent-handoff/PURE_LINUX_ENGINE_AUDIT_2026-10-26.md。

### Phase 1：建立静态依赖图

只对用户合法准备的匹配二进制做只读检查。收集 file、readelf -h -l -d -Ws、nm -D、objdump -T 输出；DT_NEEDED、未解析符号、JNI 导出、JNI_OnLoad；JavaVM、AssetManager、Android logging/system properties、dlopen/dlsym 引用；线程/TLS、文件路径、时钟、网络、图形 API；已知构造、初始化、tick、动作和状态读取入口的引用者、前置状态、调用约定、返回语义。

输出 dependency-report.json 与 DEPENDENCY_MAP.md。每项标记 STATIC_FOUND、RUNTIME_OBSERVED、PROVEN_REQUIRED 或 UNRESOLVED。符号名只是线索，不能直接推断依赖必需性。不得公开二进制、私有地址/寄存器原始日志或资源。

### Phase 2：验证 QEMU user-mode 与 ABI

先用最小 AArch64 Linux ELF 验证进程启动、syscall、线程、时钟、文件 I/O、IPC、signal、退出码和日志。这只是 smoke test，不是游戏成功。

然后验证 Android ELF 的动态链接方式、Bionic sysroot、Android linker 与未解析符号。固定 QEMU 版本和 SHA。不得将普通 Linux ELF 通过写成 libg.so 通过；不得将合成程序通过写成原生游戏证据。

输出 QEMU_ABI_FEASIBILITY.md 与独立 smoke tests。

### Phase 3：确认最小游戏生命周期

从匹配版本的既有 probe 出发，对每个候选入口建立独立、最小、可复现的实验：GameState 构造/初始化/载入/tick/销毁；battle manager / replay controller 的创建与生命周期；内容数据、卡牌、投射物、效果目录初始化；原生快照；action/logic command 构造、校验、排队和应用。

现有 frozen/android_reference_probe.cpp 包含 GameState 和 ReplayBattleController 候选偏移；它们必须和当前二进制 SHA/机器码签名绑定。每个实验记录输入 canary、返回值、调用前后状态摘要、未解决 API、资源释放和退出状态。重点判断能否绕开 UI 场景管理器创建 GameState/battle manager，还是必须由 GameApp/Replay scene 建立隐式全局状态。

### Phase 4：构建 Linux host 与 IPC

仅当 Phase 1–3 有证据支持时开始。组件建议：linux_engine_host（管理生命周期、IPC、计时、日志）；arm64_engine_worker（QEMU user-mode 下的 ARM64 进程）；android_abi_shim（只包含已证明必需的兼容函数）；native_battle_adapter（转换 Nashura 的 Action/Observation）；protocol（优先 Unix domain socket，消息含版本、请求 ID、tick、engine SHA、状态摘要）。

最低命令：PING、CREATE_MATCH、RESET、STEP_TICKS、APPLY_ACTIONS、OBSERVE、STATE_DIGEST、TERMINATE。STEP_TICKS 必须由原生 tick/update 入口推进，不能靠 sleep 或改游戏时间变量冒充。APPLY_ACTIONS 返回 accepted/rejected 和原因。OBSERVE 只在决策边界序列化，避免逐 tick JSON 开销。

第一阶段每个 worker 每进程一局；确认没有进程全局状态泄漏后再做多 worker 并行。进程崩溃、协议错序、状态摘要不一致时必须 fail closed，不得自动切到模拟器继续训练。

### Phase 5：正确性对照

对同一个引擎构建、相同初始状态、相同合法动作流，比较双方可见观测、圣水、手牌轮转、部署生效 tick、实体创建/销毁、位置、HP、目标、攻击、弹道、塔状态、动作接受/拒绝、计时、加时、终局原因和胜负。

先比每个决策边界，不要只比最终胜负。随机性记录 seed 与随机源；不能控制同一随机流时，将确定性子轨迹和随机差异分开。至少完成空场/等待、单卡部署、交战、法术、建筑、塔摧毁、加时/终局、非法动作等场景。

状态标签：
- PURE_LINUX_SMOKE_PASS：只有进程/ABI smoke test；
- NATIVE_MATCH_LIFECYCLE_PASS：原生引擎在 Linux 中完成一局；
- NATIVE_PARITY_LIMITED_PASS：仅明确列出的轨迹/机制通过；
- NATIVE_PARITY_FAIL：存在差异；
- BLOCKED：缺输入或依赖未解决。

这些标签不能互相替代，也不能自动关闭 G0/G1 或批准正式训练。

### Phase 6：吞吐基准与 10× 门槛

先做正确性，再测速度。对当前 Android 模拟器路径和 Linux host 使用相同硬件、相同固定动作轨迹、相同完整终局条件。不要把作者自写 simulator 的 658× 实时与原生引擎吞吐混为一谈。

至少测 completed matches/second、simulated game-seconds/wall-clock second、ticks/second、每局平均与 P95、reset/startup 成本与纯推进成本、CPU/内存峰值、完整终局率、动作拒绝率、单 worker/2/4/8 worker 吞吐与扩展效率。记录原始数据、运行环境、commit SHA、引擎 SHA 和日志 SHA。

每配置多次重复并报告中位数与离散程度。只有 Linux 路径达到 Android 路径至少 10×，且完整局和正确性门通过，才记为 PERFORMANCE_10X_PASS。否则如实报告倍率与瓶颈。

## 6. 交付物与回执

先完成审计再决定是否实现 host，不要先铺开大量 shim。必须创建：

1. agent-handoff/PURE_LINUX_ENGINE_AUDIT_2026-10-26.md：仓库现状、视频核验状态、依赖图摘要、可行性结论。
2. agent-handoff/PURE_LINUX_ENGINE_PLAN_2026-10-26.md：调用链、初始化对象图、shim 列表、IPC 协议、阶段门和性能设计。
3. agent-handoff/CODEX_REPLY_2026-10-26_PURE_LINUX.md：逐项标记 IMPLEMENTED、UNIT_PASS、NATIVE_VERIFIED、BLOCKED 或 NOT_RUN，附 commit SHA、实际命令、退出码、日志 SHA 和未解决问题。

公开检查、主工程检查、source-manifest 和凭据扫描必须按仓库规则执行。公开报告不得包含私有二进制或原始地址/寄存器审计。

最终原则：先证明原版战斗状态能在无 Android 进程的 Linux host 中创建、推进、观测和销毁；再证明语义正确；最后才追求 10× 吞吐。不要把“无渲染”当作“脱离 Android”。
