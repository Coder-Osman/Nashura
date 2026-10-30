# x86 Linux 服务器：Android 与 ARM Linux 转译对比

日期：2026-10-30。状态：`PAIRED_X86_TRANSLATED_MATCH_MEASURED_LIMITED`。
整体 headless 目标继续进行；`HEADLESS_BATTLE_READY=UNVERIFIED`。

## 正式结果

同一台双路 Xeon Gold 6130 服务器，在相同逻辑 CPU 4、5 上顺序运行，
两者是同 socket 的不同物理核。每路线一个战斗进程、30 局；每局均真实
tick3601 finalized=1、终态3091字节摘要6c959326dd2f09cc，trace=0。

| 指标 | ARM Linux + QEMU user | Android x86_64 + native bridge |
|---|---:|---:|
| 每局 step 循环中位耗时 | 2145.899 ms | 2471.171 ms |
| 全部计时窗口聚合吞吐 | 1675.878 tick/s | 1452.439 tick/s |
| 对局吞吐 | 0.4654 局/s | 0.4033 局/s |
| 每局单 step p50 的中位值 | 432.486 us | 487.848 us |
| 每局单 step p95 的中位值 | 1393.978 us | 1628.346 us |
| 每局单 step p99 的中位值 | 2469.597 us | 2817.502 us |
| 引擎进程 getrusage 峰值 RSS | 354.50 MiB | 357.96 MiB |
| 局后 smaps RSS 样本最大值 | 355.43 MiB | 360.18 MiB |
| 局后 smaps PSS 样本最大值 | 355.25 MiB | 239.93 MiB |

本配置 Linux + QEMU 的计时窗口吞吐高15.38%，中位耗时低13.16%。
这些结果不是进程启动、资源初始化、reset、终态校验、RPC或完整训练吞吐。
step循环还包含探针的计时、tick读取和finalized检查；不是裸函数微基准。
百分位是各局百分位的中位值，不是把所有step合并后重新计算的百分位。

## 配置与公平性边界

- 服务器x86_64，2 socket、32物理核、64线程，约250GiB内存。
- Linux：QEMU aarch64 user 8.2.2、真实ARM64 Bionic/ART、直接入口。Docker
  cpuset=4,5、memory=6GiB、network=none、pids=256；无privileged、Binder或图形设备。
- Android：API30 Android11 Google APIs x86_64 r16；KVM x86_64 AVD，
  2vCPU、6144MiB、无窗口，emulator37.1.11。全部现有emulator线程固定到4,5，
  新线程继承；真实ARM64 JNI通过libndk_translation.so执行。
- Android系统图形进程属于对照环境，不构成Linux战斗依赖证据。
- 正式v2先暂停本实验AVD，完成QEMU全部进程后恢复AVD，再运行Android。
  两个正式窗口无交叠；v1有短暂竞争，保留但不纳入正式统计。其他服务器任务未停止。
- 两边6GiB只是配置/限额一致：容器内存限额与虚拟机RAM不是相同内存机制。
- QEMU运行的是ARM ART解释执行，显式-Xint、-Xusejit:false、禁用image生成，
  堆16..256MiB。Android由x86 ART和ARM native bridge分工，debuggable测试应用。
  两套ART、JNI诊断和执行模式不同，15.38%仅是这两套实际配置的比较。
- QEMU用真实pthread承载ART入口，解决host主栈maps与guest mprotect边界；
  未伪造syscall或ART成功。Android仅跳过ABI不兼容的自有JNI注册观察和信号采样，
  保留原始引擎JNI、VM slot/getter检查和真实终局。

## 正确性和最小依赖

固定同一libg、APK、原始DEX、八卡组seed及52条root.cmd，由原loadReplay装载。
两路线独立trace预检均生成3602帧、12,334,754字节，归档SHA256相同：
`e5bc727424104a14059519ae097bccc6389137cbd1ae97397abea9eea40da206`。
这与先前ARM64 Linux/Android归档一致；覆盖显式原始serializer adapter的状态，
不包括未序列化全局、其他fixture或原客户端snapshot wrapper。终局仍有未来命令，
不把52条装载说成全部apply。

Linux只保留四个基础设备、只读proc和现有property文件，没有启动原始init、
system_server、SurfaceFlinger、Display或GPU/EGL服务。历史闭包仍含工具及
DT_NEEDED库，尚未逐文件/逐库裁剪，不宣称文件闭包已最小化。

Android最小Context补入真实getClassLoader：原Mainloop确实调用该接口，
ContextWrapper向空base委托曾触发CheckJNI终止；现返回探针实际ClassLoader。
Instrumentation在线程中准备本地Looper以满足真实Activity构造器；没有重建服务。
ARM libc内部setenv用于native bridge环境传递，而非修改主机或系统全局环境。
UnityAdsTitan缺类与storeKeyValue空回调日志保留，不升级为客户端功能通过。

## 内存和启动口径

进程RSS/PSS包含转译器和运行库的影响；Android共享页分摊与独立QEMU进程不同。
局后RSS样本晚于getrusage记录，可略高于该行峰值，两者不是同一采样时点。
host每250ms采样，QEMU进程RSS最大357.42MiB，整个emulator进程最大
6805.41MiB；后者含虚拟机RAM、Android系统和模拟器，不能与应用RSS混排。
此差异支持当前部署成本观察，不等于已测并发密度、整机内存增量或容量上限。

一次正式批次从调用到进程退出，Linux约131.967s，Android约112.079s，
相应30局step窗口合计64.462s / 74.378s。它们的初始化、资源提取、
收尾和ART缓存状态不同，且只有一次批次，不作为冷启动统计；
可见只看step吞吐不能推出整个短批次的总耗时更低。
服务器并发密度未测。本机旧入口45.421 / 45.685ms来自其他硬件及不同VM配置，
不将跨机器倍数归因为转译器单一成本，也不混入本表。

## 为什么比本机慢很多

本机为Apple M4 Max、ARM64原生执行；服务器为Xeon Gold 6130、ARM64引擎
经动态转译执行。Linux实际对局的跨机中位耗时比47.25倍，Android为54.09倍。
这是硬件、指令集、转译器及运行时配置共同变化后的观测值，不是转译器单独倍率。
Android的KVM执行x86_64系统，原始ARM64 libg仍通过native bridge转译；
因此两条服务器路线均涉及ARM到x86指令转换。

计时分解：Linux30局wall合计64.462s，其中直接step调用区间64.179s；
Android74.378s中直接step73.999s。外围循环仅占0.439% / 0.510%。
空计时探针5轮各100万次，服务器原生x86两次clock_gettime及循环约81.42ns，
ARM经QEMU约810.01ns；按每局3601步折算约2.92ms、占Linux对局0.14%。
计时调用和外围观察不是此次秒级耗时的主要来源。
host每250ms的进程CPU采样按同一monotonic时钟对齐Linux30个计时窗，
插值约65.05 CPU秒/64.46墙钟秒，接近持续占用一个核。含采样误差及
进程内其他线程，不是精准热点比例，但不支持主要时间都在外部服务等待的解释。

同一服务器、同一物理核、同源-O2合成探针，5轮中位结果：

| 每轮2000万次依赖迭代 | 原生x86 | ARM64经QEMU | 转译/原生 |
|---|---:|---:|---:|
| 整数、分支与表访问 | 87.996ms | 174.422ms | 1.98x |
| 标量double、分支与表访问 | 88.891ms | 1324.331ms | 14.90x |

整数摘要和double输出逐轮一致；浮点禁用编译器FMA收缩。编译器及目标代码不同，
这是辅助诊断，不是libg自身的原生x86对照，也不是Android native bridge微基准。
它证明转译成本显著依赖运算类型，不能统一假定为几个百分点。
本机同源ARM64探针整数102.220ms、double119.610ms，线程未固定核心，
与服务器编译器不同；该合成工作负载未显示服务器原生硬件存在几十倍差距，
但不外推到引擎的缓存、向量、分支或其他热点。

服务器idle频率曾为1GHz、governor=powersave；独立短负载采样升至约2.8GHz。
没有证据支持整个对局被锁在1GHz，不能把idle读数当作慢速根因。Docker无CPU
quota，仅cpuset两核；真实step在单个Mainloop上串行，服务器总核数不自动加速一局。

当前最有证据的判断是转译路径值得优先分析；真实47~54倍中有多少属于转译、
浮点语义实现、内存访问、单核硬件或运行时仍待真实热点分析。尚无原生x86 libg
样本，不能宣称精确拆分。perf_event_paranoid=4导致perf计数器访问被拒，
未修改系统配置；保留该失败，后续采用现有权限内的采样或其他可复验分析入口。

## 失败与复验

保留QEMU主线程栈保护失败、property属主导致读取失败、Android native bridge
JNI诊断hook崩溃、缺原始依赖库及空base ClassLoader等历史。修复均针对已观察边界。
新增统计校验器先验证完整30局、顺序、trace=0、真实finalized、终态摘要、
正常Java完成和PM零拒绝，再计算结果；13项正负向测试通过。
最终源码NDK严格构建和Java8/D8构建通过；纯Linux1175 passed、1 skipped，
主工程211 passed；公开检查结果见机器摘要，不把工程测试替代原生完整对局。

追加v56identity ARM64 guest真实完整复验：bootstrap、catalog前后、battle前后
五处原始root/context/wrapper/singleton关系和getter均一致，跨阶段稳定；
完整对局退出0，3602帧归档仍为相同SHA256。它只证明对象图身份，
不代替完整owner初始化语义、严格catalog门或其他资源类型覆盖。

机器摘要：`x86-server-headless-comparison-evidence-20261030.json`。
源码及复验方式：`tools/pure_linux/server_android/README.md`。
第三方APK、DEX、ELF、镜像和原始日志仍留在私有实验目录。

后续替代转译器完整对局和原QEMU同口径复测见
[独立实验](ALTERNATIVE_TRANSLATORS_2026-10-30.md)；本报告原30局数据保持原始身份。
