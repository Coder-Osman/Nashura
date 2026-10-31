# Android AVD 与 ARM64 Linux guest 同引擎吞吐对照

日期：2026-10-31

状态：已完成基线对照；优化工作暂停。

## 结论

在本轮 headless 固定负载下，两种运行环境的吞吐接近。Android ARM64 AVD 汇总为
82,865 tick/s，Fusion Ubuntu ARM64 guest 为 82,379 tick/s；AVD 高 0.59%。两轮反序
执行分别高 0.81% 和 0.38%，差异不足以证明有实际意义的稳定优势。

这不是 macOS 原生运行与 Linux VM 的直接对比。`libg.so` 是 Android ARM64 ELF，依赖
ART/Bionic；本轮比较的是 Android ARM64 AVD 与运行 ART/Bionic 闭包的 Ubuntu ARM64 guest。

## 测试口径

- 固定同一 `libg.so`、APK/DEX、observer、JNI probe 和四份 replay；两轮测试顺序反转。
- 每轮每平台四个输入各串行重复 30 局；每个平台共 240 局、680,700 tick。
- AVD 为 API 30 ARM64、2 CPU、6144 MiB；Fusion Ubuntu ARM64 guest 为 2 vCPU、6144 MiB。
- `libg.so` SHA-256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 优化标记为空；计时模式禁用 observer hook 与在线动作，correctness replay 单独采集 serializer 帧，并且全程串行。计时覆盖 warm step loop，初始化、reset、终局校验和 warmup 不计入吞吐时间。
- 探针未创建 EGL/GPU context；结果反映此 headless CPU 路径，不反映渲染性能。

## 吞吐

| 轮次 | 平台 | 测量局 | tick | 计时合计 | 吞吐 |
|---|---|---:|---:|---:|---:|
| 1 | Android ARM64 AVD | 120 | 340,350 | 4.094310 s | 83,128 tick/s |
| 1 | Ubuntu ARM64 guest | 120 | 340,350 | 4.127314 s | 82,463 tick/s |
| 2 | Android ARM64 AVD | 120 | 340,350 | 4.120241 s | 82,604 tick/s |
| 2 | Ubuntu ARM64 guest | 120 | 340,350 | 4.135746 s | 82,295 tick/s |
| 合计 | Android ARM64 AVD | 240 | 680,700 | 8.214551 s | 82,865 tick/s |
| 合计 | Ubuntu ARM64 guest | 240 | 680,700 | 8.263060 s | 82,379 tick/s |

每个平台、每轮覆盖 deck A/B 与 seed 1/7。计时合计是各匹配的 warm-step wall 时间之和，
不是整个实验的端到端运行时长。

## 正确性与边界

四份 replay 的跨环境 serializer payload 和已观测状态逐帧相同：

| 输入 | 最终 tick | 对比记录数 | 结果 |
|---|---:|---:|---|
| deck-a-seed-1 | 3601 | 3602 | 全帧相同 |
| deck-a-seed-7 | 3601 | 3602 | 全帧相同 |
| deck-b-seed-1 | 2154 | 2155 | 全帧相同 |
| deck-b-seed-7 | 1989 | 1990 | 全帧相同 |

帧相同只覆盖 serializer 输出及已观测字段，不涵盖进程或全局状态中未序列化的内容；
四个输入也不足以代表所有卡组和局面。该实验没有验收整体 headless readiness、在线动作、
长期稳定性或 GPU 路径。AVD/guest 速度差小于 1%，本结果不构成更换运行环境的依据。

原始测量、APK、运行库和完整逐帧归档保留在本机实验目录，不随公开仓库发布。v1 摘要
校验失败因 `libfmod.so` SHA 被截短；修正后的 v2 输入清单和测量已通过核验，原失败证据
仍单独保留。
