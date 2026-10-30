# ARM64 串行动作与状态更新归因（2026-10-31）

本阶段在既有 ARM64 Ubuntu guest（2 vCPU / 6144 MiB）独立诊断原始战斗路径。
保持系统配置、引擎、资源、两个八卡组、seed 1/7 与固定非空动作不变。
不测并发，不扩张 Android 系统或图形服务；整体温热性能目标继续进行。

## 实现与计时范围

--native-phases 默认关闭，与 benchmark、动态手牌记录及所有 native 优化候选
互斥。首局前精确核对八处原始指令，在进程内安装近距离分支岛；原始 libg 文件
保持原样。分支岛只使用空隙地址提示，检查实际映射和每处 ±128 MiB 可达范围，
不使用覆盖映射。代码页 RW→RX，刷新指令缓存，并保留原来的 BL 与尾跳 B。

包装器调用原始目标，不重写引擎函数。间接调用将原 x8 目标传入包装器，再按
调用点真实参数转发。六窗口分别为 state update、world update、object collection
tick、command queue，以及 virtual slot +0x18 的 mode 2 / mode 3 dispatch。
32 层有界嵌套栈统计 inclusive/exclusive、调用数、最小/最大值、原始零返回值、
零时长样本和目标变化。exclusive 只扣除已插桩的子窗口。

| 调用点 | 原始目标或语义 |
|---|---|
| +0x1062144 BL | +0x1062274 state update |
| +0x10623e8 BL | +0xcef550 world update |
| +0xcf04e4 BLR x8 | object collection tick，实际目标 +0xf26410 |
| +0x1062378 BL / +0x10625d8 B | +0xd2f37c command queue |
| +0xd2fadc BLR x8 | mode 2、flag 0，本批未走到 |
| +0xd2f838 / +0xd2fdfc BLR x8 | mode 3、flag 0，实际目标 +0xd30a00 |

这些是固定 ELF 反汇编及真实运行确认的调用窗口名称，不冒充全部原始类名或
业务语义。mode 3 返回零只为原始状态，未据此判定动作成功。

此运行保留逐 tick serializer 和关键状态观察；时钟、包装器与统计开销全部包含。
**结果只作归因，不作无插桩性能基线或优化收益。** 解析/入队发生于 step 之前，
用于拆分输入路径，不计为连续 warm step 本体。正式 benchmark 继续关闭本诊断、
Frida、REA、Ghidra、strace、逐 tick 快照和详细日志。

## 四组完整局验证

每组顺序运行新 observer 原始局、原始 cmd 诊断局、将相同命令移到 inject 的
诊断局；再与历史原始 pair-a 全帧及已观测状态比较。共 12 个新完整局，全部自然
退出 0、到原生终局、Java 入口正常返回、PM 拒绝计数为零。

| case | 固定动作 | 终局 tick | 每局 serializer 帧 | 每局已观测状态行 | mode 3 dispatch / 零返回 |
|---|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 52 | 3601 | 3602 | 730 | 50 / 48 |
| deck-a-seed-7 | 51 | 3601 | 3602 | 761 | 49 / 47 |
| deck-b-seed-1 | 22 | 2154 | 2155 | 539 | 22 / 20 |
| deck-b-seed-7 | 19 | 1989 | 1990 | 531 | 19 / 17 |

每组两个诊断路径的 dispatch 数、零返回数相同；前两组终局仍有两条未来命令，
后两组队列为空。输入命令总数不直接当作 apply 次数。mode 2 没有命中，不外推
其耗时或语义。队列守恒、全部原始帧和已观测状态均通过。

主机取回原始日志和全帧后，独立重算 12 个全帧比较、四组四路径状态逐行比较、
八份阶段汇总，以及 state/world/object/queue/dispatch 的嵌套计时恒等式。
这些只覆盖固定 fixture 的 serializer 和已输出字段，不代表所有内部状态等价。

## 诊断归因

下表为原始 cmd 路径的一次完整局 inclusive 毫秒数；嵌套列相互包含，不能相加。

| case | state | world | object collection | queue | mode 3 dispatch |
|---|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 30.284 | 28.883 | 28.376 | 0.826 | 0.526 |
| deck-a-seed-7 | 33.144 | 31.655 | 31.166 | 0.873 | 0.562 |
| deck-b-seed-1 | 15.361 | 14.695 | 14.383 | 0.321 | 0.199 |
| deck-b-seed-7 | 14.511 | 13.900 | 13.618 | 0.281 | 0.202 |

八个诊断局中，object collection 窗口约占所测 state inclusive 的 92.94%–94.03%，
queue 约占 1.94%–2.97%。这是含插桩开销的窗口归因，不等同无插桩 CPU 比例。
它与之前 warm-window CPU profile 的 state/world 调用链方向一致，支持下一步
优先检查对象遍历、组件更新和临时内存分配，而非重建外围 Android 服务。

inject 路径逐项解析累计约 6.958–24.374 μs，追加累计 0.500–1.167 μs；
全部发生在 step 前，没有逐动作分布或持续在线输入测量。cmd 解析包含在原始
GameState load 中，未虚构独立的 cmd parser 时间。

## 失败与修正

- 初次 v1 打包/部署失败，runner import 缺失，未运行引擎；记录单独保留。
- v2 四组均未通过汇总：两组安装返回 task -56 后外层超时 124；两组完整终局，
  但 queue 的 min_ns=0 被原校验器错误拒绝。原安装器没有细分 syscall 错误，
  不将 -56 的具体失败步骤当作已观测事实。
- v3 改为从进程 maps 空隙寻找近分支岛，检查实际 mmap 返回地址，不覆盖映射。
  增加 guard/island 失败标记及零时长次数，接受计时分辨率内相同时间戳，仍验证
  非负值、范围、调用数与嵌套计时。cmd 四组 queue 零时长次数为 57/27/45/318；
  它们不是零成本操作。
- v3 12 局和主机复算通过；保留 v2 失败目录，不把重试写为首次成功。

## 身份、检查与边界

- libg SHA：110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783。
- observer SHA：430804674efc5c848e43a600b468471ea5857b1755e48e4b74c00628fdd6e180；
  NDK Android30 ARM64、-O2 -g -Wall -Wextra -Werror 构建通过。
- 私有 v2/v3 原始归档：11,624,927 字节，SHA
  a7701282c38e1b5b683ed749c67d8dd5c0ce6015817212016a358d88a042db52。
  包含日志、设置、全帧和失败；二进制、原始私有日志及第三方资源不公开。
- pure_linux：1266 passed / 1 skipped；公开检查：1104 passed / 42 skipped；
  主引擎：211 passed；gitleaks 通过。清单结果见机器摘要。没有新增性能收益结论。
- 一次 cmd 与一次 inject 诊断/fixture，不是统计性能实验。近分支岛仍可能因地址
  空间布局安装失败，失败即拒绝任务；没有修改 VM 地址随机化或其他系统配置。
- 未新增硬件 cycles/cache、分配次数、线程同步/off-CPU 或长期稳定性证据。
  整体性能目标继续 active，下一步沿真实 +0xf26410 对象/组件路径归因。

完整计数、源身份和检查记录见
[机器摘要](serial-arm64-native-phases-evidence-20261031.json)。
