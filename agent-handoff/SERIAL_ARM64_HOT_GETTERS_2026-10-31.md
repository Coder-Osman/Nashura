# ARM64 串行热 getter 内联候选（2026-10-31）

本阶段继续固定 ARM64 Ubuntu guest、2 vCPU / 6144 MiB 与串行非空动作对局。
不运行并发测试，不扩张 system_server、SurfaceFlinger、GPU/EGL、Display 服务。
整体 headless 性能目标仍进行中；`hot-getters-v1` 为默认关闭的限定 build 实验。

## 运行环境与 QEMU 边界

本机为 Apple Silicon ARM64。2026-10-31 只读检查 Mac 及 guest 的进程表，未发现
QEMU 或 Android Emulator。Mac 上现存的 `vmware-vmx`（检查时 PID 5010）承载
既有 VMware Fusion Ubuntu guest；它与 QEMU 是不同进程。当前 ARM64 guest
直接执行同架构引擎指令，没有 QEMU user-mode 指令翻译层。历史 x86 服务器的
QEMU 测量和 Android AVD 记录不属于本机当前热路径。

Linux guest 仍用于现有 Android ELF、Bionic 与 ART 运行环境；ARM64 指令同构
不等于 macOS 可以直接加载 Android ELF。当前阶段仍依赖 ART/JNI、资源适配和
原始运行时依赖闭包，未宣称已完成无 ART 的 macOS 原生移植。

## 候选与适用边界

依据既有 warm-step profile 和精确 ELF 的反汇编，三个热调用者中的 11 个 BL
只调用单条字段 LDR 加 RET 的 getter。候选把调用点替换为 getter 原有 LDR，
读取时机、字段宽度和字段偏移不变，不缓存、不提前读取、不重排。

| getter | 原始语义 | 调用点数量 |
|---|---|---:|
| `+0xcf0654` | `ldr w0,[x0,#0x188]; ret` | 2 |
| `+0xf1b9e0` | `ldr w0,[x0,#0x11c]; ret` | 2 |
| `+0xf171b4` | `ldr x0,[x0,#0x48]; ret` | 6 |
| `+0xd98ca4` | `ldr x0,[x0,#0x260]; ret` | 1 |

调用者为 `+0xf66e30`、`+0x115d540`、`+0xf5d7e0`；在对应调用之前保存 LR，
返回前恢复 LR，中间正文没有其他 LR 读取。`+0xf2d68c` 不是单条 load getter，
已排除。等价主张限于正常执行语义，不覆盖 signal/profiler 调用栈或 fault PC。

安装前校验三调用者入口、11 条原始 BL 和四 getter 的 LDR/RET；全部通过后
在首局之前改动一次性进程代码页。去重页面先 RW、写指令、刷新指令缓存，再 RX；
原始库文件保持不变。`--native-optimization hot-getters-v1` 显式启用，
与 `spatial-y-bound-v1` 互斥；runner 要求唯一且精确的安装成功标记。

## 四组完整局正确性

原始输入沿用已冻结的两个不同八卡集合、seed 1/7 和固定非空动作。每组分别
运行新 observer 的原始路径与优化路径，再对照上阶段原始路径。三条路径的
全部 serializer 帧及已观测状态逐行一致，未用单个终态 digest 替代全帧验证。

| case | 动作数 | 原生终局 tick | serializer 帧数 | 已观测状态行数 |
|---|---:|---:|---:|---:|
| deck-a-seed-1 | 52 | 3601 | 3602 | 730 |
| deck-a-seed-7 | 51 | 3601 | 3602 | 761 |
| deck-b-seed-1 | 22 | 2154 | 2155 | 539 |
| deck-b-seed-7 | 19 | 1989 | 1990 | 531 |

该门只覆盖固定 build、四个输入和已输出字段，不证明其他卡组、玩法或所有
未序列化内部状态等价。性能批次仅做终态 tick/digest/长度与串行 reset 验证，
不冒充每局全帧对照。

## 串行性能对照

正确性门通过后启动同 observer 的 A/B/B/A/B/A/A/B 测量；每个 case 8 批、
每批 16 局 warmup 加 100 局测量，计划共 32 批 / 3712 局 / 3200 测量局。
计时仅 warm step loop，每 16 tick 相位轮转抽样；关闭 Frida、REA、Ghidra、
strace、profiler、逐 tick snapshot、详细状态日志和动态手牌动作。
主机工程测试在全部计时退出后运行。批次作为统计单位，重复局不是独立样本；
主机频率、热状态及虚拟机调度未锁定。

全部 32 批正常退出，3712 局原生终局和串行 reset 校验通过。主机独立复算全部
原始日志、输入身份、安装标记及四组全帧/观测状态；同一 case 的所有批次输入和
observer 完全相同，只有显式优化开关不同。

下表耗时为四个批次中位数的中位数；吞吐按四批总 tick / 总 warm-loop 时间。

| case | 原始耗时 ms | 优化耗时 ms | 耗时变化 | 原始 tick/s | 优化 tick/s | 吞吐变化 |
|---|---:|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 28.999 | 27.290 | -5.89% | 123934 | 131267 | +5.92% |
| deck-a-seed-7 | 31.672 | 29.925 | -5.52% | 113382 | 120238 | +6.05% |
| deck-b-seed-1 | 13.996 | 13.265 | -5.22% | 152458 | 162301 | +6.46% |
| deck-b-seed-7 | 13.376 | 12.708 | -5.00% | 147418 | 155928 | +5.77% |

分别计算每个 ABBA / BAAB block 内两批中位耗时的平均值，四组耗时变化为：
`-6.15% / -5.42%`、`-6.14% / -5.38%`、`-4.60% / -6.13%`、
`-4.88% / -4.91%`。每组四个优化批次的中位耗时均低于其四个原始批次；
这些重复交错测量支持当前 guest 和四 fixture 的局部收益，不作更大总体的显著性
或长期稳定性结论。每 variant/case 只有四个批次，不能把 3200 局当作独立样本。

step 分位数为各局抽样分位数的批次汇总，再取四批中位数；不是 pooled 原始 step：

| case | 原始 p50 / p95 / p99 μs | 优化 p50 / p95 / p99 μs |
|---|---:|---:|
| deck-a-seed-1 | 5.521 / 17.917 / 24.229 | 5.417 / 16.125 / 21.500 |
| deck-a-seed-7 | 6.041 / 18.438 / 22.688 | 5.917 / 16.562 / 21.042 |
| deck-b-seed-1 | 5.813 / 10.125 / 11.854 | 5.500 / 9.334 / 10.958 |
| deck-b-seed-7 | 5.146 / 12.750 / 14.937 | 5.031 / 11.625 / 13.594 |

所有批次 sampled RSS 峰值约 258.2–259.0 MiB；不由该观测声称分配次数或内存
效率改善。没有新增硬件 cycles/cache、action apply 独立阶段或 off-CPU 等待计数。
候选继续默认关闭，不改变冻结部署，也不合并空间查询候选。

## 证据身份与检查

- libg SHA：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 新 observer SHA：`ff659f6ac3d24496f76c2517b2c3911a16f22a59b446128a6ac3c7bebfbcc4f3`。
- 私有归档 6783043 字节、307 个文件；SHA：
  `eb7c851ce6d51310bb4dfd274627edb71866cefbab9939f3ff7f91e2bdef3d23`。
  原始日志、全帧、设置和二进制保留本地，公开文件只包含自有实现、哈希和摘要。
- NDK clang 使用 `-O2 -g -Wall -Wextra -Werror` 构建成功；主机再次核对
  11 个 BL、四 getter 的 LDR/RET 和三个完整函数体中的 LR 保存/恢复。
- Python 3.13.5：pure_linux `1249 passed / 1 skipped`；公开检查
  `1104 passed / 42 skipped`；main_engine `211 passed`，controller 均退出 0。
  源码清单、gitleaks 与 diff 检查结果记录于机器摘要。
- 归档时无 benchmark、正确性或引擎 workload 进程残留；guest 配置未变化。

完整批次统计、终态身份和原始输出哈希见
[机器摘要](serial-arm64-hot-getters-evidence-20261031.json)。后续仍需动作解析/
入队/apply 与状态更新归因、分配/同步与 off-CPU 证据、更多 fixture 和长期稳定性；
当前结果不替代整体 headless readiness 或最终性能目标。
