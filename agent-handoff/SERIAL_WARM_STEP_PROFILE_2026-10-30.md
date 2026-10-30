# ARM64 headless 温热串行 step 基线与 seed 复验

日期：2026-10-30

状态：`SERIAL_WARM_STEP_VALIDATED_LIMITED`

整体目标完成：否

## 结论

在既有 Ubuntu ARM64 guest 中对固定 headless runner 做了单进程串行测量，没有运行并发组。ART/JVM 初始化后，每个 replay 仍销毁并重建 GameState；指标只计每局温热 step 循环。固定 seed 1 的独立基线为每局中位 28.088 ms，吞吐 127,336 tick/s；按每 16 tick 采样得到单 step p50/p95/p99 为 5.500/21.835/72.858 微秒，共 6,780 个样本。30 局均到 tick 3601，serializer 终态为 `6c959326dd2f09cc`、3,091 字节。

另一批相同输入运行 30 局，同时在 runner 子进程完成前后采集 Python `RUSAGE_CHILDREN` 累计值。该批每局中位 27.930 ms，30 局与 serializer 检查通过；runner 子进程的 user/system CPU 计数差为 1.803/0.152 秒，minor/major faults 为 78,072/0，voluntary/involuntary context switches 为 1,436/39。step 计时窗与该累计资源口径不同。两次运行不是严格交错的配对对照，其时间差不作为性能提升。

固定双方八卡组和 52 条命令，只改变 JSON 顶层 `rndSeed`，分别以 seed 7、19、41 顺序各运行 30 局。各组内部都稳定复现同一终局 serializer：

| Seed | 终局 tick | Digest | 字节数 | 每局中位耗时 | step p50/p95/p99 (微秒) |
| --- | ---: | --- | ---: | ---: | --- |
| 7 | 2846 | `530ed75967cbc6e0` | 3207 | 15.733 ms | 3.813 / 12.146 / 15.584 |
| 19 | 3214 | `aac146c6e46ea067` | 3406 | 23.174 ms | 5.063 / 16.688 / 44.751 |
| 41 | 3601 | `5b4f9d94dbb4c59f` | 2828 | 24.184 ms | 5.250 / 15.688 / 27.209 |

不同 seed 产生不同合法终局，不能横向比较每局耗时或 digest；summarizer 现在可传入 fixture 专属的 expected tick、digest 和字节数，并继续验证 30 次重复内部一致。

## 方法与证据

- Guest：Ubuntu 22.04.5 ARM64，2 vCPU，6 GiB；所有运行单进程、按序执行。
- 固定 replay SHA-256：`fec6da6425549e1c62ef3709a3517826b4000ee27f98566122d0966ec6e0d0f8`；包含 52 条命令。双方卡组均为 `[26000000, 26000001, 26000005, 28000001, 28000000, 26000003, 26000014, 26000018]`。
- seed 7/19/41 replay SHA-256 分别为 `e89244779a3bbf64bcf733a50a985c537a9cfb3c11b7b96cbd3863b2bce15e83`、`dbb4296fb56f36fde352479f3a9a3d6b2ff9327fb26bf1b5e48c7aa00bfb61f8`、`685025f478c2d15817914342c204fd878ba7e03aa028cf39acfd4a0bffcd5ac6`。结构化比较确认除 `rndSeed` 外输入完全相同。
- Observer SHA-256：`b5dc8e2a6d40f5b6fb5c6034186682e85a6c97a3954bc1214759850043adb015`；engine SHA 与既有 profile 一致。
- Benchmark 使用 `NASHURA_BENCHMARK_MINIMAL=1`、30 次 reset、每 16 tick 采样、内存采样开启、snapshot trace 关闭。step 分位数是各局分层采样分位数的中位数，不是逐 tick 全量分位数。
- perf 的软件计数器和采样需要 guest sudo；非交互执行要求认证且 SSH 未启用，因此没有生成新 perf 数据。硬件 cycles/instructions/cache 事件仍不可用。原始失败日志留在本机私有临时证据中，不发布。
- 严格 summarizer 专项测试 `20 passed`；公开工程检查 `1104 passed, 42 skipped`，主引擎检查 `211 passed`，源码清单校验 2,882 项，gitleaks 通过。
- serializer readiness 保持 `UNVERIFIED`，snapshot trace 未捕获，未宣称完整状态或 Android 等价。

## 性能与覆盖边界

旧 profile 的主要热点仍在第三方 `libg.so` 的二维空间格候选查询；本轮没有取得新的调用栈 profile 来进一步归因，也没有改写或分发第三方二进制。当前仓库没有第二套已验证为同引擎版本、且其命令与 selection 都匹配的八卡组 replay，故本轮只覆盖一套卡组的三个 seed。没有应用引擎优化，数字是新串行基线，不代表相对旧版本的加速。

下一步需要先得到真实可验证的其他卡组 replay 与原生 selection 输入，再做单进程串行复验；同时为受限 guest 建立经校验的被动调用栈采样路径，之后才能筛出仓库自有热路径和验证优化收益。整体 headless readiness、多个卡组覆盖和模型强度均未完成。
