# Codex 执行任务：脱离 Android 模拟器的高吞吐对局引擎

- 日期：2026-10-25
- 仓库：https://github.com/Coder-Osman/Nashura
- 工作分支：codex/headless-engine-plan-20261025
- 目标：验证能否在不运行完整 Android Emulator 的条件下推进《皇室战争》对局；交付可复现的工程证据与吞吐基准。
- 本文是工程任务，不是启动正式训练的授权。

## 1. 成功定义

必须区分三种方案：

1. 关闭渲染但仍使用 Android Emulator：可作为当前优化，不算“脱离模拟器”。
2. 在宿主机直接运行原版 ARM64 战斗逻辑：只有证明初始化、合法动作、状态观测、tick 推进与自然终局均有效，才算原生 headless 成功。
3. 自有 headless 模拟器：直接由 CPU 状态机推进对局，不启动 Android 客户端；必须披露尚未复刻的规则与 sim-to-real 差距。

优先限时调查方案 2；若依赖阻断或没有真实完整局证据，转向方案 3。动态库可加载、单个函数可调用、QEMU 可运行合成程序，都不等于完整游戏已运行。

## 2. 执行约束

1. 只在本分支和独立工作树工作，不直接向 main 提交、不强推、不重写历史。
2. 不杀死、重启、修改或覆盖任何冻结部署、Full128、terminal 或远端运行任务；运行状态不明时，按仍在运行处理。
3. 本任务不授权启动正式 1,280 局迁移、G2/G3、正式自博弈训练或多机放量。
4. 不把 fixture、合成状态、CPU 单测、静态分析或 QEMU 合成程序伪装成原生对局证据。
5. 不提交 APK、libg.so、提取资源、第三方引擎二进制、模型权重、凭据、真实机器清单、私有对局或原始内存/寄存器审计。
6. 保持 FAIR 可见性和单信念契约，不得向策略泄露对手隐藏手牌、私有 RNG 或未来状态。
7. 不为追求速度擅自增加训练 seed。基准测试使用固定工程样本，与训练实验分开。

## 3. P0：审计视频与当前仓库

### 视频

用户提供：https://www.bilibili.com/video/BV1CKHV6wEyf/

若能访问视频、字幕、简介、置顶评论或作者公开代码，记录其真实方案：复用原版引擎还是自写模拟器、是否使用 QEMU、动作/观测接口、时钟推进方法、是否真正摆脱 Android Emulator、吞吐如何测量。若无法核验，写明 VIDEO_TECHNIQUE_UNVERIFIED，禁止猜测作者实现；不要因此阻塞其余工作。

### 仓库

记录 git status --short --branch、git rev-parse HEAD、git rev-parse origin/main（若可访问）、现有测试基线和所有工作区的实际路径。检查 README.md、CHANGELOG.md、TRAINING_ENGINE_PLAN.md、docs/REPRODUCING.md、wire-spec.md 和 firstlight_CR/native_runner/。

重点确认以下内容究竟在当前主线还是历史隔离副本，不得直接假定已集成：

- _spec_work/versions/fair-final-history-v2/project/native_runner/sim_core/
- _spec_work/versions/fair-final-history-v2/project/native_runner/cr_native_env.py
- firstlight_CR/native_runner/cr_native_env.py
- firstlight_CR/native_runner/battle_env.py

仓库历史文档说明原版引擎位于 ARM64 libg.so，并曾提出重写训练专用引擎。历史 sim_core 原型只覆盖速猪 2.6 的 8 张牌，不是完整 122 卡引擎。README 在 2026-10-25 记录 G0/G1 仍开放、G2–G8 未启动，但执行前必须重新检查最新状态。

交付：agent-handoff/HEADLESS_ENGINE_AUDIT_2026-10-25.md，记录 commit SHA、文件路径、证据等级、现状、依赖与风险。

## 4. P1：限时调查原版 ARM64 引擎的 headless 可行性

只对用户合法准备的匹配二进制做只读检查。可使用 file、readelf、nm、objdump 等工具。

### 4.1 静态依赖图

记录 ELF ABI、动态链接器、DT_NEEDED、导入/导出符号、JNI 入口、线程/TLS、文件/网络/时间依赖，以及 Android 系统库、AssetManager、EGL/GLES、Surface、Audio、Java/Looper 等依赖。识别可能的初始化、动作、tick、状态读取入口；必须标明哪些是静态推断，哪些有实际执行证据。缺少匹配文件时写 BLOCKED_MISSING_INPUT，不得用别的版本代替。

### 4.2 最小启动探针

依次评估：
- 直接构造原生 harness；
- ARM64 Linux user-mode/QEMU 加最小必要运行时；
- 复用现有 probe / 原生对象初始化路径的 headless host。

最低通过条件：启动新局、执行合法动作、推进多个 tick、读取真实状态，并自然到达终局。不可随意 stub 未知逻辑、伪造胜负或忽略动作拒绝来制造成功。

### 4.3 决策门

审计报告必须给出以下唯一状态之一：
- NATIVE_HEADLESS_FEASIBLE：有可复现的真实完整对局证据；
- NATIVE_HEADLESS_BLOCKED：依赖链与实际实验表明在合理预算内无法可靠剥离必要 Android 依赖；
- NATIVE_HEADLESS_INCONCLUSIVE：缺少二进制、环境或实际运行证据。

附命令、退出码、日志 SHA、依赖缺口和下一步。只有 FEASIBLE 才能提议把它作为主后端；不允许仅凭理论判断宣布成功。

## 5. P2：实现自有 headless 模拟器 MVP

### 5.1 接口与隔离

在真实主线中核实 BattleEnvV1 / NativeClashEnv 的契约，再增加独立后端，例如 SimClashEnv。用显式配置选择 native、sim，或仅在 P1 通过后选择 native_headless。默认保持当前后端；禁止静默切换。

至少实现 reset(seed, match_config)、step(actions)、observe(player)、is_terminal()、export_trace()，并保留动作拒绝、tick/决策边界、终局原因、观测版本和引擎身份。不得改变模型、FAIR 搜索、Replay 格式或训练准入门。

### 5.2 先复用原型，完成 8 卡闭环

审查历史 sim_core，确认可移植性后再迁移；不得直接复制整个历史工作区，也不得把历史测试当作当前主线已验证。先支持原型范围的速猪 2.6 八张卡，跑通脚本对脚本完整对局。明确列出未实现的机制，至少检查：

- 圣水、计时、手牌轮转、部署延迟、位置合法性；
- 寻敌与目标优先级、移动、碰撞、攻击前摇/攻速、弹道和伤害；
- 法术范围、建筑寿命、死亡效果、塔激活/摧毁；
- 加时、平局/终局、随机机制、双方同 tick 动作顺序；
- 观测可见性、动作拒绝和决策边界。

不支持的机制必须显式报错或禁止，不得静默忽略。随机机制必须有显式 seed 与隔离随机流；如果当前 MVP 暂未实现某种 RNG，必须把它列为限制，不能宣称原生等价。

### 5.3 性能优化顺序

先正确，再加速。依次测量并优化：
1. 不渲染、不产生逐 tick 大日志，只在决策边界生成观测。
2. 进程常驻；reset 重置状态，不重复加载模型/资源。
3. 用 profiler 找热点，再减少对象分配、全量排序和重复计算。
4. 确认对局之间完全独立后，批量并行多个 match；每局状态、RNG、事件、终局完全隔离。
5. 热点明确后再考虑 C++/Rust、Numba 或向量化；不要先做 GPU 仿真或牺牲确定性。
6. 解耦策略推理批处理与环境推进，保证每个动作对应正确的 observation version 和决策边界。

MVP 只要求单进程单局确定性闭环；并行批处理是后续阶段。

## 6. P3：正确性与原生对照

### 工程测试最低集合

- 相同 seed 与动作流重复运行，状态摘要、事件摘要、终局逐位一致；
- reset 后无上一局实体、手牌、事件、计时或 RNG 泄漏；
- 双方同 tick 动作顺序符合契约；
- 非法动作拒绝、延迟生效、终局后 step 行为明确；
- 不同环境实例之间状态、RNG、文件、端口隔离；
- 一次性推进与分段 step 在每个决策边界一致；
- 观测不泄漏隐藏信息，schema 与训练接口一致。

### 原生对照

若能使用匹配的真实原生引擎，按固定初始配置和动作流比较每个决策边界：双方可见观测、圣水、手牌、部署时刻、实体创建/销毁、位置/HP、攻击/弹道、塔状态、动作接受/拒绝、时钟和终局。先按机制分层，不可只比较最后胜负。

无真实原生基准时，必须标记 NATIVE_PARITY_NOT_RUN；公开视频、fixture 或合成测试不能替代。结论只能是：
- SIM_INTERNAL_CONSISTENCY_PASS：仅内部一致性；
- NATIVE_PARITY_LIMITED_PASS：仅明确列出的机制/轨迹通过；
- NATIVE_PARITY_NOT_RUN：未做真实对照；
- NATIVE_PARITY_FAIL：有未解决差异。

这些结果都不能自动推进 G0/G1 或 G2–G8。

## 7. P4：可复现的吞吐基准

正确性 MVP 通过后再测吞吐，不启动正式训练。对相同固定配置、动作轨迹和工程 seeds，分别测量当前原生路径与新后端。原生路径不可用时，明确报告无对照，不伪造速度倍率。

至少报告：
- wall-clock seconds / completed match；
- completed matches / second；
- simulated game-seconds / wall-clock second；
- ticks / second；
- 每局平均与 P95 延迟；
- CPU 利用率、内存峰值、启动成本、reset 成本；
- 单局及 2/4/8 局并发吞吐、扩展效率；
- 完整终局率、错误/拒绝率、轨迹 SHA 与引擎身份。

每项性能重复测量并报告样本数与离散程度。分别记录“正确性对照”和“速度对照”；只测得一次或动作流不同，不能声称加速。

## 8. 最终交付与验收

先交付以下文件，再考虑扩大实现范围：

1. agent-handoff/HEADLESS_ENGINE_AUDIT_2026-10-25.md：视频核验、依赖图、候选方案、P1 决策门。
2. agent-handoff/HEADLESS_ENGINE_PLAN_2026-10-25.md：后端接口、规则覆盖矩阵、sim-to-real 验收计划与吞吐测试设计。
3. 可运行的 8 卡 MVP、独立测试与 benchmark 脚本（若审计后仍值得继续）。
4. agent-handoff/CODEX_REPLY_2026-10-25_HEADLESS_ENGINE.md：逐项状态、文件路径、commit SHA、实际命令与退出码、测试结果、失败与未完成项。

回复中逐项使用 IMPLEMENTED、UNIT_PASS、NATIVE_VERIFIED、BLOCKED 或 NOT_RUN。附上源 SHA、测试日志 SHA、吞吐原始数据位置和限制说明。

完成目标后，按 AGENTS.md 更新必要的 README、docs/RESULTS.md、CHANGELOG.md 和发布证据；运行仓库要求的公开检查、主工程检查、source-manifest 校验及凭据扫描。公开报告不得包含私有二进制或审计内容。

**本任务最终目标不是“写一个看起来能跑的模拟器”，而是得到一条不依赖 Android Emulator、可复现、可测量、对 FAIR 搜索语义透明的对局推进路径。先证明，再优化；先闭环，再扩到完整卡池。**
