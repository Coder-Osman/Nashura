# ARM64 headless 串行 warm-step 采样相位复核

日期：2026-10-31

状态：`BENCHMARK_SAMPLING_METHOD_VALIDATED_LIMITED`

整体目标完成：否

## 结果

本轮只改进仓库自有 benchmark 观察器与汇总器，没有修改 `libg.so` 或战斗行为。固定
replay 在每次 GameState 重建后从相同 tick 相位采样，会使 30 次重复始终落在同一组
tick 模 16 位置。现在按串行 match index 轮转 phase，并在 native metrics 行明确记录
`sample_phase` 与 `sample_phase_period`。汇总器验证 phase 顺序、有效周期和相位对应的
样本数；interval 大于本局实际 step 数导致零样本时，native 路径执行完整资源释放后
以失败状态退出。

在 ARM64 Ubuntu guest 上以固定 seed 1、同八卡组、52 条原生 replay 命令运行单进程
46 局：前 16 局作为 warmup，后 30 局计入统计。采样间隔 16 tick，周期 16；计入统计
的 30 局覆盖 phase 0..15 后再覆盖 0..13。phase 0 每局 226 个样本，其余 phase 每局
225 个样本，共 6,752 个 step 样本。46 局全部原生 finalized 到 tick 3601，终态
serializer 摘要均为 `6c959326dd2f09cc`、3,091 字节，进程退出码 0。

| 指标 | 结果 |
| --- | ---: |
| warm match 中位耗时 | 68.672 ms |
| match p50 / p95 / p99 | 68.666 / 70.160 / 70.216 ms |
| 吞吐 | 52,404 tick/s |
| sampled step p50 / p95 / p99 | 12.813 / 43.960 / 62.773 μs |
| 单局 step 样本数中位数 | 225 |
| 测量批次 step 样本总数 | 6,752 |
| 峰值 RSS | 258.86 MiB |

step 分位数是各测量局分位数的中位数；p10/p90 跨局范围分别为：step p50 10.375–
11.958 μs、p95 33.710–48.460 μs、p99 44.335–114.712 μs。计时范围只含 warm step
loop；ART/JVM 初始化、reset、serializer 终态校验和 16 局 warmup 不计入汇总值。

## 环境与复核

- VMware Fusion 中的 Ubuntu 22.04.5 ARM64 guest，2 vCPU、6,144 MiB；执行前 load
  average 为 `0.02 / 0.03 / 0.00`，可用内存 2,578 MiB，无 swap。
- `libg.so` SHA-256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
  replay SHA-256：`fec6da6425549e1c62ef3709a3517826b4000ee27f98566122d0966ec6e0d0f8`。
- 新 observer SHA-256：`39c5a13103daeb41eaee3455c92a6e37a9e0c0fedf5d013f68b3342821f80ca7`。
  使用 NDK r27.3.13750724、Android API 30 ARM64 target、`-O2 -Wall -Wextra
  -Werror -Wl,-z,defs` 构建。
- Frida、REA、Ghidra、strace、逐 tick snapshot trace 和逐 tick 详细日志关闭；
  每局只在计时窗结束后读取一次 memory rollup。
- stdout SHA-256：
  `2be8304bbcc01f0f1b9374cba719620b2213731e8e65d897206b19882647905e`；计划 SHA-256：
  `41ac2ac7553ed187b79e578a4d6c0bdd1b8c3adaabd6c772a34a88b36dc20022`；exit 为 `0`。
  完整脱敏字段见[机器证据](serial-arm64-sample-phase-evidence-20261031.json)。
- Python 3.13.5 下纯 Linux `1189 passed、1 skipped`，公开各组累计 `1104 passed、42 skipped`，
  主引擎 `211 passed`。首次用系统 Python 3.9.6 运行纯 Linux 全集时有 16 项因缺少
  `cryptography` 失败；切换到机器已安装的 Python 3.13.5 后完整重跑通过，没有安装依赖。
- 公开源码清单校验 `2885` 项通过，`gitleaks` 无发现，`git diff --check` 通过。

## 可比性与边界

本轮 warm match 中位 68.672 ms 与 10 月 30 日旧报告的 28.088 ms 差异明显。旧报告
使用不同 observer / 计时实现；本轮两次独立 46 局运行的中位数也分别为 60.461 ms
和 68.672 ms。guest vCPU、内存、引擎和 replay 身份相同，但当前证据不能把差异归因
于采样 phase 代码或虚拟机调度，故两组只作为独立记录，不形成性能前后对照，也不
宣称优化收益。后续性能优化前应先锁定 observer 构建和主机/guest 调度状态，取得
同 observer 的稳定配对基线。

本阶段没有运行并发 workload。只有一个八卡组和一个 seed 的复测，没有取得新的
CPU call-stack、cycles、cache、内存分配或线程同步 profile；动作解析/队列/apply 的
阶段时延也未新增。没有改写第三方引擎。多卡组完整对局、serializer 全量状态门、优化
前后配对与整体 headless readiness 仍未完成。
