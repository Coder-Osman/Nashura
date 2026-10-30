# ARM64 headless 串行 CPU 热点归属

## 结论

固定 replay 的 ARM64 headless workload 中，串行 `cpu-clock:u` profile 捕获 2,569 个样本，零丢样。热点函数范围为 `libg.so+0xf34b3c..0xf35064`，共命中 294 个样本，占全部样本 11.44%；单个最热指令 `+0xf34c60` 命中 53 个样本。

同一 ARM64 ELF 的 AArch64 反汇编显示，该范围把定点坐标按 `2^10` 缩放到二维格坐标，遍历格区间、读取候选对象列表并执行对象状态/虚调用过滤，随后维护结果集合。基于指令形态，将其归为“空间格候选查询”是有证据的行为推断；具体业务函数名和它代表碰撞、寻敌或其他玩法子系统尚未确认。

静态调用边界为 `+0xf5d9d8 -> +0xf34afc`，随后 `+0xf34b2c -> +0xf34b3c`。动态样本出现在 `Mainloop` 线程的固定 replay profile 中，说明该查询落在本次实际执行路径内。它没有单独证明某个命名的 `GameState::Step` 符号，也不构成多 seed 覆盖证明。

## 工作负载与证据

- ARM64 guest，单进程、单 vCPU 串行执行；没有运行并发测量。
- 60 次固定 replay/reset，单局 3,601 tick、载入 52 条命令，终态摘要 `6c959326dd2f09cc`（3,091 字节）。这是单 fixture 的重复 workload。
- 性能基线独立汇总 10 次热身和 50 次测量：每局中位数 28.280 ms，p95 30.306 ms。profile 采样受 profiler 开销影响，不与该基线做性能增减比较。
- profile 使用软件事件 `cpu-clock:u`，2,569 个样本，0 lost samples。guest 映射中的 `libg.so` 身份与本地分析副本匹配，SHA-256 为 `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- FDE 边界为 `+0xf34b3c` 至下一函数入口 `+0xf35064`；范围长度 1,320 字节。局部符号缺失，按 guest 映射和 ELF 地址恢复偏移。
- Python 3.13.5 下纯 Linux 全集 `1183 passed、1 skipped`，公开检查合计 `1104 passed、42 skipped`，主引擎 `211 passed`；公开快照校验和 gitleaks 通过。系统 Python 3.9 的首次收集失败是解释器不兼容，失败 JUnit 留在本机。

机器可读摘要见[证据文件](serial-arm64-cpu-profile-evidence-20261030.json)。转换后的 perf script（含 callchain）与映射文本、第三方 `libg.so` 留在本机临时目录，没有进入公开快照；原始 guest `perf.data` 未能取回。

## 离线样本复核

2026-10-30 对保留的 `perf script` 与 `PERF_RECORD_MMAP2` 文本做了离线复算，没有重跑 workload。2,569 个事件头均解析出 sample IP；2,079 个事件属于 `Mainloop`，1,890 个 sample IP 映射进 `libg.so`。查询函数范围内的 294 个 sample IP 全部来自 `Mainloop`，热点指令计数复现为 `+0xf34c60`/53、`+0xf34c7c`/43、`+0xf34c78`/40、`+0xf34c90`/35、`+0xf34c98`/27。复算输入 SHA-256 分别记录在机器证据中。

VMware Tools 拒绝读取 guest 内原始 `perf.data`，返回文件权限错误；本次复核只使用已保留的脚本与映射文本，没有修改 guest 文件。较早的 v2 报告虽有 988 个样本，但缺少可核对的 run plan、raw data 和映射 ELF 身份，不与本 profile 合并或用作代码归因依据。

## 适用边界与后续

这是一个固定 replay、一个 ARM64 guest、一个串行 profile 的热点定位结果。硬件 PMU 的 cycles/instructions/cache 事件不可用；软件 CPU 时钟样本不能解释具体指令周期、缓存未命中或因果耗时。没有执行代码优化，也没有做多 seed、不同卡组或并发验证；整体验收和模型强度状态不变。

后续只读调用者静态审计见[独立报告](SERIAL_ARM64_QUERY_CALLER_AUDIT_2026-10-30.md)：查询泛化为二维空间格候选过滤，但具体玩法语义仍未知。下一步需要沿虚回调身份与实际 battle object 做串行、被动关联；多 seed/卡组覆盖仍待补齐。只有定位到仓库自有 headless 代码中的可优化开销且能证明语义不变，才进入前后性能比较；不改写或分发第三方引擎二进制。
