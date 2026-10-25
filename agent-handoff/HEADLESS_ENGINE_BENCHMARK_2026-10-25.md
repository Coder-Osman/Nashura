# 原版 headless：限定正确性与吞吐基准

执行日期2026-10-26；仅任务分支，不修改main或冻结部署，不启动训练。

## 结论与范围

- IMPLEMENTED：可运行的独立进程入口、SHA绑定、端口归属核验、持久会话、原子结果与所属进程回收。
- NATIVE_VERIFIED：原字节ARM64引擎经QEMU Linux user-mode和Android用户态运行时，完成固定工程完整局；不启动完整Android Emulator。
- UNIT_PASS：27项独立工具/验收器测试，退出0，0.04秒；fixture测试不代替原生证据。
- P3限定原生契约通过；新Android同轨迹对照为 NATIVE_PARITY_NOT_RUN。
- P4固定工程基准完成；相对Android Emulator速度对照 NOT_RUN，无此倍率。
- P2自写sim回退 NOT_RUN：P1已实际可行，不为本任务无条件重写已有8卡原型。

运行依赖QEMU、Bionic、ART/JNI、AssetManager、匹配资源和原版战斗库；不是宿主ISA直接执行或零Android依赖。所有私有二进制、完整观测、地址与对局留仓库外。

## 样本、身份与计时

固定同一工程fixture，SHA256为51bc65212c77803858350b38f13a23bee042d2770388c2e0f40e89158fe8b3d8。
它是历史standard八卡：Knight、Archers、Minions、Arrows、Fireball、Giant、Musketeer、MiniPEKKA，不是速猪全机制覆盖。每局26次合法动作，tick3601自然终局、winner1、finalized且队列为空。

主基准主机：x86_64，64逻辑CPU，Intel Xeon Gold 6130 @ 2.10GHz。并非独占整机测试，不以64核标称数量推算吞吐。原版库SHA为110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783，其余输入身份见JSON。

ordinary模式每250tick及动作提交边界观察；每1000tick和终局仍抓完整原生事件，逐条核对26条消费回执。每档3轮，常驻进程反复reset；端到端墙钟含reset、推进、观测和校验，不含首次启动。样本为固定轨迹重复测量，不是多个独立随机seed。

真实基准producer SHA为ae43d50fe7e0f1d137169dc1288c0fa26298f51b98296bd4618b7506b03946d2；当前源码补充原子写、协议检查、元数据及回收记录，未把其身份冒充旧producer。后续P3原生复验使用当前源码48d271bf42a9db51bad626ff98c64293ea02dc6cac53b852c63044aa8acb24c5。

## 重复测量结果

| 并发 | 完整局数 | 局/秒 | 摊销秒/局 | 单局均值秒 | 单局P95秒 | tick/秒 | 游戏秒/墙钟秒 | 扩展效率 |
|---:|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 3 | 0.1574 | 6.353 | 6.344 | 6.503 | 566.82 | 28.34 | 100.0% |
| 2 | 6 | 0.3004 | 3.329 | 6.553 | 6.816 | 1081.68 | 54.08 | 95.4% |
| 4 | 12 | 0.5121 | 1.953 | 7.522 | 7.955 | 1844.00 | 92.20 | 81.3% |
| 8 | 24 | 0.8025 | 1.246 | 9.145 | 10.066 | 2889.65 | 144.48 | 63.7% |

共45局全部完整，记录动作拒绝0，所有ordinary边界和终局摘要一致。8路相对1路总吞吐5.10倍；单局均值反而增长，不能把摊销秒/局当作单局延迟或FAIR搜索加速。P95采用观测样本顺序统计量；1路仅3局，尾延迟估计证据有限。

| 并发 | 每轮墙钟标准差秒 | 每轮吞吐中位数局/秒 | 子进程CPU核数等效 | 占64逻辑CPU比例 | 最大单进程VmHWM KiB | 启动均值秒 | reset均值/中位数毫秒 |
|---:|---:|---:|---:|---:|---:|---:|---:|
| 1 | 0.1389 | 0.1592 | 0.892 | 1.39% | 372068 | 36.247 | 42.68 / 50.01 |
| 2 | 0.1460 | 0.3037 | 1.680 | 2.63% | 371828 | 36.410 | 70.35 / 54.02 |
| 4 | 0.1552 | 0.5140 | 2.898 | 4.53% | 372560 | 36.468 | 74.05 / 63.19 |
| 8 | 0.1526 | 0.7998 | 4.579 | 7.15% | 372220 | 36.563 | 75.21 / 63.50 |

CPU为测量期间全部本工具原生子进程CPU秒/端到端墙钟秒，不含Python协调器，不是整机利用率。内存是单进程生命周期VmHWM，约363MiB，不是PSS或整机总内存。启动每档每进程仅一次，总15个样本；reset共45个样本，原始值完整保留。

## 正确性证据

1. P1：两次rich完整局，独立归档验收器逐字段比较29边界，状态/事件/位置/HP/手牌等完全一致。只重定位明确的跨局generation/stateEpoch与遥测ID偏移，保留因果引用和相对间隔。
2. P3：原生拒绝step 0且状态摘要不变；reset后旧snapshot拒绝；快照往返摘要一致且释放后handle为0。
3. P3：5tick分段的两次完整局，与250tick推进的全部ordinary边界及终局SHA完全一致：trace为5f60c3b420bec6e4aead27b5dc02535c6efadbff4908a9b6bcf300eca6a9b978，terminal为fe457c3bf3c77b778e19bc5a55bf2567692262263611916f90ebebe437754037。
4. P4：独立输出目录/端口/原生进程，并发1/2/4/8下均重复一致。
5. 清理：核验本任务30条进程身份记录，所属进程及监听残留均0；重新核验原库/probe/bootstrap/DEX/verifier/QEMU/shim，全部身份一致。未调用冻结任务管理器或向冻结进程发送信号。

上述证据只覆盖固定轨迹。没有新增Android同轨迹对照；没有完成一般非法卡/坐标、100ms–3秒计划、同tick双策略隔离、所有RNG、FAIR私有字段遮蔽或训练schema验收。工程完整观测不送入策略；训练准入仍保持关闭。

## 富观测基准与失败

rich模式另有1/2路各3轮、共9局，摊销秒/局54.30/34.20，单局均值54.29/67.51秒。4路启动时人工中止本任务工具以切换观测口径，公开结果明确为partial。rich包含私有逐边界大归档，ordinary没有；二者不作公平速度倍率对照。

首次ordinary只终局抓完整事件，26动作只有24条保留回执，工具拒收；修为中途抓取后才取得主基准。另保留回执tick误加1、遥测ID直接比较失败、空事件摘要错误，以及本次控制端脚本语法/补丁上下文失败。失败不改写成成功，不修改冻结引擎。

## 复现与公开证据

需使用用户私下准备、身份匹配的运行时和固定fixture；公开仓库不附带第三方资源或私有对局。入口见tools/HEADLESS_NATIVE.md。

主基准命令：python tools/headless_native.py --runtime "$PRIVATE_RUNTIME" --resident "$PRIVATE_RESIDENT" --output "$NEW_OUTPUT" --fixture "$FIXTURE" --probe-sha256 "$PROBE_SHA256" --bootstrap-sha256 "$BOOTSTRAP_SHA256" --dex-sha256 "$DEX_SHA256" --verifier-sha256 "$VERIFIER_SHA256" --concurrency 1,2,4,8 --repeats 3 --observation-ticks 250 --capture-mode ordinary。实际退出0。

分段原生命令使用相同固定输入，改为--concurrency 1 --repeats 2并附加--step-chunk 5 --protocol-check；退出0。

公开文件：publication/headless-benchmark.json、headless-rich-benchmark.json、headless-p3-native.json、headless-cleanup.json。含实际producer、输入、fixture、日志/结果SHA及每轮原始指标；原始富观测留私有证据区。

## 后续边界

先做同轨迹Android新对照及训练动作/FAIR适配验证，再考虑显式后端接入；这不是自动执行授权。本任务未修改默认后端，不开放G0/G1、34/43或G2–G8，不启动迁移/自博弈/正式训练。
