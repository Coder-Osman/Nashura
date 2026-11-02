# SSPI：当前状态与新增事件协议

## 实现与启用

本轮在现有 JSON 原子捕获协议上增加显式增量模式，不依赖压缩，不替换为缺少完整语义的 compact 客户端。

- 控制连接命令：`observe-atomic-delta generation epoch combat phase visibility remaining special action character`。参数是九个 uint64；负号、溢出、缺项、尾随内容均拒绝。
- resident 批量连接：`batch-json-delta-v1` 握手，flag bit 2，每槽请求附加 72 字节小端游标；与 compact delta 互斥。
- 当前 ordinary/rich 对象、手牌物理槽、当前运行时事实完整返回。七个事件环只返回已确认 `nextSequence` 之后的新记录，仍携带 epoch/oldest/transmitFrom/next/overflow/rejected/complete 等校验字段。
- generation/epoch 变化从新身份的 retained window 开始；同身份倒退、缺口、非法记录失败关闭。游标仅在完整原子捕获校验通过后提交，且按客户端、resident 槽独立保存。
- 宿主不重建旧完整事件。combat 仅保留当前原生保留窗口内的 kind、target/projectile ID 和必要 deployment descriptor，用于跨批因果、重复出牌、描述符一致性验证；其余环保留游标。Tracker 和战场语义仍维护必要当前摘要。
- 正常 `collect_episode`、CLI、代际 experiment 通过 `transport="event-json-delta"` 使用增量 actual，belief 使用 `persistent-delta`。旧 transport 默认保持，避免新代码自动向未升级 probe 发新协议。新运行必须记录 transport/source/probe 身份，既有冻结 run_config 不在原地改写。

独立客户端断线重连保留已确认游标。成功 reset/configure/restore 才重置；失败 restore 保留原游标。批量 step 发生错误不自动重放推进。`CR_FAST_ENV`、跳过 JSON 校验和压缩混用在增量路径被拒绝。

## 真实搜索发现并修复的回归

首次增量搜索在第一个非空 combat 批次之后触发 `delta skipped the merge cursor`。根因是没有启用公开出牌重建的 legacy 客户端，其 `_public_card_play_next_sequence` 仍参与最小消费游标计算。现仅实际启用的消费者参与下界，保留真正的缺口检查；增加启用/禁用两种回归测试。

另一个性能问题是新控制命令每次分配和清零完整响应缓冲区。已改为与旧全量命令相同的进程级缓冲区复用；原生命令串行执行，不减少语义和验证。最终 probe 与初版 probe 分别记录 SHA，结果不混写。

## 验证口径

真实完整诊断使用固定速猪、CPU 单线程、随机初始化 seed 20261103、0% MCTS、相同 run/game/random-stream，按相同轨迹比较 full/delta。逐捕获验收使用两次独立同轨迹运行：每个原有捕获边界只读取一次，再计算当前状态与各环新增事件的摘要。仅归一化跨运行的 generation/epoch、各环事件序号基点，以及不随 match 重置的进程级 deploymentSequence 基点；保留相对关系，不排除战斗字段，不额外发送原生请求。审计额外计算摘要，因此其墙钟不纳入速度结论。

最终审计从 tick90 到 tick4801，共435个捕获边界：当前状态、七类新增事件、完整规范化 combat 记录及564条动作逐项一致。首次独立审计遗漏 deploymentSequence 基点，133个出牌边界的 combat 摘要因此不同；保留该次证据，补齐基点归一化后独立重跑全量和增量，最终一致。公开复现入口见 publication/sspi-json-delta-20261102；仅使用独立空闲测试 worker。

128 搜索对照在一个独立进程中先冻结实际开局 FAIR 证据，再独立创建 belief，不复制实际隐藏状态。两版比较全部候选及顺序、先验、Q、访问数、所选动作、FAIR/模型身份、真实推进 tick、快照往返与句柄释放。它是单进程顺序工程验收，**不代表双进程实际根物理状态保留验收，也不代表混合采集完整训练闭环**。

测量保留顶层互斥阶段和嵌套 RPC/推理时间。解码字段含 JSON 解析与完整协议校验；request_ack 嵌套在 receive，不重复求和。原生拒绝导致的 quarantine 不记成平局或有效训练数据。

具体数值和完整身份见 `SSPI_JSON_DELTA_EVIDENCE_2026-11-02.json` 与 `RESULTS.md`。仅发布源码和摘要，probe 二进制、权重、原始私有捕获不发布。

## 观测副作用审计

首次尝试同 tick 连读 full/delta，在 tick129 发现三个 movementStep 字段变化。源码确认旧 `read_native_phase_snapshot` 先读缓存，再调用 `g_original_phase_effective_speed`，该 getter 经已安装 movement-scale hook 更新缓存，因此第二次读取的遥测现场变化。失败现场保留，未放宽字段检查，改用上述独立同轨迹、同捕获次数审计。这个既有观测副作用另列后续问题；本轮增量和全量调用次数保持一致。

## 部署事故与限制

第二次重载 Engine5 时，执行 `am set-debug-app -w` 触发 Android 对整个包的 force-stop，旧 Engine0–4 亦被终止。这是操作错误，非协议崩溃。调用前无训练/评估 actor；旧 Engine0 槽2已终局 tick4801，其余旧进程为空闲控制端点。进程内对局和快照状态丢失，落盘 episode/checkpoint、AVD、adb、原转发、全局 APK/probe 文件保留。

已保留 logcat，清除包级 debug 设置，重新启动已有 Engine0–4 组件；重启后的端点不冒称原状态恢复。新 probe 只挂载进 Engine5 的独立 mount namespace。后续只使用新鲜诊断局，不把跨事故结果当同部署的连续运行。

后续任何 worker 重载禁止使用包级 `am set-debug-app`；应先确认调试启动是否按进程生效，逐个核对存活身份。正式混合采集前仍须通过 actual＋两个 belief 的相同 probe 身份和完整观测预检。
