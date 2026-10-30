# ARM64 headless warm-step 窗口归因与串行 task 等待修复

日期：2026-10-31；来源分支：`codex/pure-linux-libg-headless-20261026`。

状态：`WARM_WINDOW_ATTRIBUTED_AND_SERIAL_WAIT_VALIDATED_LIMITED`。
整体目标完成：否；native 计算优化与性能收益：尚未取得。

## 本阶段结果

修复自有 runner 的长串行批次等待契约，完成 216 局正常退出的 ARM64 CPU-clock
profile；另在工程检查退出后，以同一 runner、observer 和输入连续执行两批无 profiler
基线，各 16 局 warmup、200 局测量。三批的所有对局均 finalized 到 tick 3601，终态
serializer 摘要 `6c959326dd2f09cc`、长度 3,091 字节，runner 正常返回、PM 零拒绝。
这证明本 fixture 下的重复终态稳定，不是 serializer 全字节或未序列化状态等价验收。

新增 `summarize_headless_perf.py`，只归因日志中 `[start_ns,end_ns)` 的 warm step
窗口。它要求纳秒精度 perf script、MMAP2、明确的 monotonic clockid、零丢样、唯一
Mainloop 进程、最小观测计划、精确引擎身份及 runner/perf/wrapper 三个成功退出码；
复用现有终局与 serializer 验证，排除初始化、warmup、reset、终态序列化和收尾样本。
计时路径没有 Frida、REA、Ghidra、strace、逐 tick snapshot 或详细 tick 日志。

## 环境与输入

- VMware Fusion Ubuntu 22.04.5 ARM64 guest，kernel `6.8.0-138-generic`，固定 2 vCPU、
  6,144 MiB；默认 CPU affinity 为 `[0,1]`，没有把单个任务固定在某个 vCPU。
- 最终基线前 load average `0.00 / 0.03 / 0.00`，MemAvailable 2,610,388 KiB，无 swap。
  没有修改 guest 内核设置、虚拟机配置或部署中的冻结文件。
- seed 1；两方同一个八卡组；固定 52 条原生命令。replay SHA-256：
  `fec6da6425549e1c62ef3709a3517826b4000ee27f98566122d0966ec6e0d0f8`。
- libg SHA-256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- observer SHA-256：`39c5a13103daeb41eaee3455c92a6e37a9e0c0fedf5d013f68b3342821f80ca7`；
  runner SHA-256：`bd275733d24ce1ffc7b47be44ef9a9e498de95c6ca72c95bf0210622cdb90b5d`。
- APK/资源容器、DEX、runtime/framework/property 输入的身份由同一个 settings 锁定；
  profile 与最终两批基线的 `identities` 和环境字典逐项相同。
- 每次只有一个 headless 引擎进程执行完整对局，各批顺序运行，没有并发 workload 测试。

## 独立速度基线

下表两列都是等待契约修复后的相同构建，**不是优化前后对照**。冷启动、ART 启动、
输入校验、资源首次加载、reset、终态校验、16 局 warmup 和收尾均不计入 step-loop
时间。每 16 tick 进行一次 step 计时，起始相位随串行 match index 确定性轮转。

| 指标 | 基线 A | 基线 B |
| --- | ---: | ---: |
| 运行局数 / 计入统计 | 216 / 200 | 216 / 200 |
| match 中位耗时 | 29.428 ms | 29.023 ms |
| match p50 / p95 / p99 | 29.424 / 32.471 / 34.789 ms | 29.022 / 31.755 / 34.855 ms |
| warm step 吞吐 | 121,324 tick/s | 122,783 tick/s |
| sampled step p50 / p95 / p99 | 5.626 / 18.461 / 25.629 μs | 5.542 / 18.189 / 24.710 μs |
| step 样本数 | 45,013 | 45,013 |
| sampled RSS 峰值 | 258.324 MiB | 258.176 MiB |

step 分位数是各测量局抽样分位数的中位数，不是 45,013 个原始 step 的 pooled 分位数。
各项跨局 p10/p90、内存与 guest CPU 时间快照见机器摘要。CPU jiffies 覆盖整个子进程
生命周期，不解释为 warm step cycles。主机核分配、频率、热状态和后台应用未锁定；
最终两批基线没有重叠本代理运行的工程测试，因此仍保留主机/虚拟机调度噪声边界。

较早两批无 profiler 记录中位为 29.322 / 28.587 ms，但与本机工程检查有时间重叠，
独立保留，不提升为最终空闲基线。10 月 31 日前一相位复核报告中的 60–69 ms 记录也
保留原意；当前没有证据把历史差异归因于某项代码优化。

## 仅 warm-window 的 CPU 归因

使用 `perf record --clockid mono -F 997 -e cpu-clock:u -g --call-graph fp`，perf 获取
采样权限，runner/引擎通过 `setpriv` 降回普通 guest 用户。使用 `perf script --ns`
保留纳秒时间戳；evlist 确认 `use_clockid:1, clockid:1`。

| 样本范围 | 样本数 |
| --- | ---: |
| 全部软件 CPU-clock 样本 | 7,428 |
| 非 step 窗口，已排除 | 1,291 |
| 前 16 局 warmup 窗口，已排除 | 448 |
| 后 200 局 step 窗口 | 5,689 |
| 其中 Mainloop / main | 5,687 / 2 |
| 窗口内 libg 叶样本 | 5,629（98.95%） |
| `+0xf34b3c..+0xf35064` 查询范围叶样本 | 949（16.68%） |
| callchain 包含查询范围的样本 | 1,033（18.16%） |

零 lost samples，后 200 局每局捕获 27–31 个窗口内样本。查询最热叶 IP 文件偏移
`0xf34c60` 命中 200 次；其余热点与 Mainloop 调用路径保存在机器摘要中。
已核对精确 ELF：上述查询所在第一个 executable PT_LOAD 的 `p_offset == p_vaddr == 0`，
所以该区间的归一化文件偏移可与既有函数范围对应。其他 segment 的 offset/vaddr
并非全部相同；通用汇总器明确输出文件偏移，不把任意映射偏移冒作函数地址。

这些在线 CPU 样本将下一步优先级指向 native 对象遍历、空间候选过滤及其调用链；
当前没有扩大 system_server、SurfaceFlinger、GPU/EGL 或 Display 适配的依据。
但 CPU 样本不包含全部 off-CPU 等待，不能据此排除线程阻塞，也不等于各函数耗时或
cache/cycles 计数。本阶段未替换 native 函数，未取得因果加速结论。

## 失败记录与修复依据

1. v1 调用了旧部署 runner 不支持的 benchmark CLI，退出 2，只有 1 个 Python 样本；
   没有启动 battle，不作运行时 profile。
2. v2 使用正确 runner 完成 46 局，取得 251 样本，但默认 perf 时钟与 step 计时窗
   尚未绑定；仅作独立 profile，未并入窗口内比例。
3. v3 设置 monotonic/997 Hz 并执行 216 局；虽输出全部逐局终态，控制线程的默认
   task 等待约五秒到期，Java 报 `-5`，runner 最终被外层 timeout 以 124 终止，
   perf/wrapper 均退出 1。7,549 个样本和异常输出保留，整批不登记通过。
4. 源码确认所有 reset repeats 共享一个 Mainloop task。runner 现为 benchmark 设置
   `NASHURA_TASK_TIMEOUT_MS = min(timeout_seconds * 1000, 120000)`；外层 timeout 继续
   独立生效。该修复只调整控制线程等待，没有修改 `libg.so`、observer 或战斗行为。
   v4 和后续两批 216 局均正常完成，验证了超过旧等待窗口的实际串行路径。

原始 perf.data、script/maps、runner plan/stdout/stderr、controller 环境快照均保留在
操作者本地目录。公开[机器摘要](serial-arm64-warm-window-evidence-20261031.json)保存
输入/输出 SHA-256、统计及失败状态，不发布第三方二进制、原始私有审计或凭据。

## 验证及未完成项

Python 3.13.5 下针对性检查 44 项通过；纯 Linux 1,216 项通过、1 项跳过，公开检查
1,104 项通过、42 项跳过，主引擎 211 项通过。公开源码清单、gitleaks 与最终 diff
检查结果见机器摘要。两项检查的 host zsh 后处理曾使用只读变量 `status` 而退出；
实际测试日志和九组公开 JUnit 均为零失败/零错误，后处理错误不记作测试成功码。

本阶段覆盖一个 seed、一个卡组和一套固定非空动作。多 seed/不同卡组、逐字节 serializer
及关键状态复验、动作解析/入队/apply/状态更新阶段归因、内存分配与同步/off-CPU
热点、硬件 PMU、长时间稳定性及实际 native 优化前后配对仍未完成。下一阶段沿
Mainloop 的 native 热点取证并评估语义保持的优化；不恢复并发测试，不扩张外围服务。
