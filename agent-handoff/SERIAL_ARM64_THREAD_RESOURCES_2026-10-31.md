# ARM64 warm-loop 线程资源与有界串行稳定性（2026-10-31）

来源为功能分支 codex/pure-linux-libg-headless-20261026，基于 436e859。
VM 继续固定 2 vCPU / 6144 MiB、CPU affinity [0,1]，引擎、资源、seed、卡组与
固定非空动作不变。不测并发，不修改系统配置，不扩张 Android 服务。整体目标继续进行。

## 线程 CPU、缺页与上下文切换

新增默认关闭的 --thread-resources，只在独立 repeated warm-loop 诊断中启用。
在 step loop 两端读取 CLOCK_THREAD_CPUTIME_ID 与 getrusage(RUSAGE_THREAD)，
不在逐 step 内添加系统调用。CPU/rusage 区间包围原始 loop，但两者边界并不完全相同；
额外边界成本单独记录。诊断不当作正式速度基线，不根据 wall-minus-CPU 推断锁或
Binder 等待原因，也不把线程 CPU 纳秒冒作硬件 cycles。

每组先复验新 observer 原始局的全部 serializer 帧及已观测状态，再独立运行 216 局
资源诊断和 216 局原计时路径。各自剔除前 16 局，留下 200 测量局，四组全部正常退出。
两套运行都关闭逐 tick snapshot、详细状态、native 阶段诊断及 profiler。

| case | Mainloop CPU / 边界墙钟 | 主动 / 被动切换 | minor / major faults | 块输入 / 输出 | 首末 RSS KiB（剔除 warmup） |
|---|---:|---:|---:|---:|---:|
| deck-a-seed-1 | 99.9865% | 0 / 46 | 30 / 0 | 0 / 0 | 264900 / 264844 |
| deck-a-seed-7 | 99.9781% | 0 / 55 | 13 / 0 | 0 / 0 | 264484 / 264412 |
| deck-b-seed-1 | 99.9696% | 0 / 19 | 7 / 0 | 0 / 0 | 264444 / 264444 |
| deck-b-seed-7 | 99.9868% | 0 / 17 | 0 / 0 | 0 / 0 | 264628 / 264628 |

这些固定输入的 warm loop 中，Mainloop 几乎一直计入线程 CPU，主动切换、major fault
和块 I/O 均未观察到；与既有 CPU-clock profile 的 native 叶样本方向一致，支持继续
优先 native 计算。它不证明所有线程、动作模式或长时负载没有阻塞。getrusage 的
user/system 微秒可能由较粗粒度内核记账，不能强行与线程时钟逐局完全相等。

四组安静复跑的每局中位数为 29.039898 / 31.448375 / 13.763995 / 13.491043 ms；
这是相同新 observer 的独立观测，不是新优化收益。完整吞吐和 sampled step p50/p95/p99
见机器摘要。资源诊断数据与安静路径绝对耗时不直接作加速对照。

## 硬件 PMU 能力核验

保持 perf_event_paranoid=4，不改变内核设置。使用现有授权凭据，仅让 perf 以 root
检查 /bin/true 的事件能力；没有以 root 启动引擎。sysfs 只列出 breakpoint、kprobe、
software、tracepoint、uprobe，无 hardware PMU。cycles:u、instructions:u、
cache-references:u、cache-misses:u 均输出 <not supported>；perf 进程仍退出 0，
因此不能把成功退出当作取得硬件计数。task-clock/context-switches/page-faults 软件
事件有数值，只作能力检查，不冒充 battle 计数。sched_switch tracepoint 存在；本阶段
没有采集其真实战斗等待调用链，未闭合精确 off-CPU 原因。

## 单进程串行稳定性

新增显式 --serial-batches；默认一个 task，显式多批时在同一个已初始化引擎进程内
重复提交相同 replay。每个 task 保持原生最多 120 秒等待，外层最多 600 秒；批次数
1..240、每批 repeats 2..1000，未放宽引擎终态门。多批不混入资源/native 诊断或全帧
记录，避免持续输出对性能路径的额外影响。每批 BEGIN/END、原生终局和正常进程退出
都必须齐全；独立汇总器核对唯一 create、同一 Mainloop TID、批间时钟不重叠、完整
终态摘要、内存样本和顺序。多个 task/局共享进程，不当作独立实验样本。

四组输入的原始路径与旧 11 点 hot-getters 各运行一个进程，共 8 进程、80 批、
41280 局，全部原生终局且进程退出 0；每批剔除 16 局，保留 40000 测量局。
相同新 DEX 下再补四组优化完整局，与新原始局及历史原始局的全部 serializer 帧
和已观测状态配对一致，共 8 个新完整局门。稳定性局本身只保留终态验证，
不外推为每局全帧证明。主机对归档原始日志、全部批次、计划/环境和身份独立复算。

| case | 路径 | 首至末 loop 秒 | RSS 首 / 末 / 峰值 KiB |
|---|---|---:|---:|
| deck-a-seed-1 | original | 154.410 | 265012 / 265000 / 265044 |
| deck-a-seed-1 | hot | 151.171 | 264760 / 264740 / 264784 |
| deck-a-seed-7 | original | 169.511 | 265528 / 265524 / 265568 |
| deck-a-seed-7 | hot | 158.258 | 264432 / 264424 / 264468 |
| deck-b-seed-1 | original | 78.021 | 264784 / 264764 / 264804 |
| deck-b-seed-1 | hot | 74.649 | 264552 / 264540 / 264580 |
| deck-b-seed-7 | original | 73.757 | 264360 / 264336 / 264380 |
| deck-b-seed-7 | hot | 71.247 | 264404 / 264364 / 264412 |

每个进程只有上表所示有限观察时间；RSS 样本未显示首尾增长，不证明无内存泄漏，
也不外推为 24 小时或无限期稳定性。原始后优化的顺序用于稳定性，不是交错性能
对照，本阶段没有新增加速结论。

首次汇总器将 createGameMain 的返回形式错误写死为 null=true；真实记录为 false 并
返回原始字符串。对局已正常结束，解析失败原样保留。修正版只要求唯一且合法的
原始 create 返回标记，并核对 task/终态/入口完整性；不伪造返回值，不重跑已完成局。

## 身份与证据

- libg SHA：110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783。
- observer SHA：ca2abc5829e82f3592de3128736b1af91979c50533cf5e110db11a88fe32e7f7。
- 多批 DEX SHA：2c31ef981e0a63a2bb595233b7fad99f0d228499e254eaa10844c567e360bc5e。
- 独立资源原始归档 3,233,992 字节，SHA：
  6b83765c6513256642dda2aab2f38c5d15f2e1a8e063f2291560f0ea6ce1e35f。
  主机核对大小/SHA，并重新比较全帧、观测、退出、计划、输入身份和原始资源日志。
- 稳定性与补充全帧原始归档 9,306,614 字节，SHA：
  44f1440a85c72dcca5c5bb4cbf70f797f254f5b77a855f8744f22439ada0b260。
  主机核对后独立重算 8 进程、80 批及 8 个完整局门；原始解析失败保留。
- 公开仅保存自有实现、脱敏统计和哈希；第三方 APK、DEX、libg、原始私有审计与凭据
  继续本地持有。完整 checks、稳定性归档和边界见同名机器摘要。
- 保留 preflight v1 对受限 tracefs 的 PermissionError 和更正后的逐项结果；未通过的
  工具操作不计作引擎验证。长期稳定性、硬件 counters、全内部状态和全 fixture 仍未闭合。

## 工程检查与保留边界

纯 Linux 1323 passed / 1 skipped；九套公开检查合计 1104 passed / 42 skipped；
主工程 211 passed。NDK Android30 ARM64 -O2 -g -Wall -Wextra -Werror 及 Java8/D8
构建通过。严格解析新增缺失/重复任务标记、迟到 create 与负 RSS 拒绝；最初测试
替换了不存在的 fixture token、随后真实日志含合法 setup task TID 的两次修正均留痕。
主机与 guest 汇总浮点末位差异仅在派生浮点数使用相对 1e-14 / 绝对 1e-12 容差；
原始计数、身份、时钟及日志哈希精确核对。资源与稳定性计数、全帧对照、原始失败
和工程检查是不同证据门。整体目标继续进行，精确等待因果及更长时间稳定性仍未闭合。
