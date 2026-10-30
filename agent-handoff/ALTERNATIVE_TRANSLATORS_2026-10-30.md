# x86服务器 ARM64 转译器替换实验

日期：2026-10-30。状态：`ALTERNATIVE_TRANSLATORS_MEASURED_NO_SPEEDUP_LIMITED`。
本轮找到能够执行完整真实对局的独立转译器，但没有测到更快的替换方案。
当前固定fixture继续保留QEMU8.2.2；整体`HEADLESS_BATTLE_READY=UNVERIFIED`。

## 实际对局结果

同一Xeon Gold6130、相同CPU4,5、同一ARM64引擎/APK/DEX/fixture、
相同直接ART入口和自有observer，依次运行arm64emu、上游QEMU、原QEMU复测。
没有同核同时测速，均无trace，逐局检查真实tick3601、finalized=1、
3091字节终态摘要`6c959326dd2f09cc`，正常Java返回和进程退出0。

| 实测路线 | 局数 | step循环中位耗时 | 聚合tick/s | 相对本轮旧QEMU耗时 | 进程峰RSS |
|---|---:|---:|---:|---:|---:|
| QEMU8.2.2发行包，同口径复测 | 30 | 2168.240ms | 1658.689 | 1.000x | 354.51MiB |
| 上游QEMU11.1.3，本地静态构建 | 30 | 3106.611ms | 1157.923 | 1.433x | 345.21MiB |
| arm64emu-user x86_64 JIT | 20 | 4857.950ms | 740.949 | 2.241x | 475.91MiB |

本次上游构建中位耗时增加43.28%，吞吐低30.19%；独立JIT增加124.05%，
吞吐低55.33%。原QEMU此前30局为2145.899ms，本轮相同observer复测为
2168.240ms，相差约1.04%，支持候选慢速现象没有由observer更换解释。
这些是固定输入、单进程串行实验；不是所有ARM程序的转译器排名。

step口径包含每步计时、tick和finalized检查，排除初始化、reset和终态校验。
每局p95的中位值：旧QEMU1399.911us、上游2058.099us、arm64emu2590.182us。
原Android native bridge的2471.171ms属于此前独立批次，见
[原服务器对比](X86_SERVER_HEADLESS_COMPARISON_2026-10-30.md)。

## 候选选择与版本

- FEX固定commit`d321f49fb5551b7e7ad0e09eee73940bf236e370`：官方说明和ELF
  loader均表明它在ARM64主机运行x86/x86_64程序。本实验主机x86_64、输入
  e_machine=183/AArch64，方向相反，因此没有编造FEX性能数据。
- 独立候选[arm64emu-user](https://github.com/sylirre/arm64emu-user)固定
  commit`90fe88ca71794d29eecddc4b8925d68b79a2c342`，实际启用`--jit`。
  原源码未改动；静态构建产物是唯一未跟踪文件，二进制身份见机器摘要。
- [上游QEMU](https://download.qemu.org/qemu-11.1.3.tar.xz)固定11.1.3，
  tag对应commit`005c61c0ef6f6ae615283440b4bb7be51b2c6ddd`。容器内只构建
  aarch64-linux-user，静态链接、默认-O2，无PGO/LTO或主机特化。
  它与原8.2.2发行包的构建配置也不同，结果不证明版本升级本身必然变慢。

## 真实正确性与最小依赖

arm64emu和上游QEMU均先独立生成完整trace，再测速。两者3602帧、
12,334,754字节归档分别经比较器确认与已有基线逐字节相同，SHA256：
`e5bc727424104a14059519ae097bccc6389137cbd1ae97397abea9eea40da206`。
终态与逐帧比较是两个独立检查；未将exit0或任务入队等同于整体验收。

保持原始引擎字节、原始loadReplay及52条root.cmd，终局仍有未来命令，
不宣称52条全部apply。覆盖显式serializer adapter，不代替原wrapper、
未序列化全局、其他fixture或完整ResourceManager语义。

全部Linux实验无网络、无privileged，cpuset4,5、6GiB内存、pids256。
没有启动init、system_server、SurfaceFlinger、Display、GPU/EGL或Binder服务。
没有修改主机全局sysctl、权限、包或userns设置。用户态闭包仍待逐库裁剪。

## 观察器适配与失败证据

arm64emu首次进入真实VM/JNI和native生命周期后，因自有探针读取
`/proc/self/mem`被拒而得到content task -3，失败日志与超时退出保留。
适配只将探针pointer读取换成guest内真实memcpy，保留版本绑定对象关系、
虚表与入口字节检查。引擎未改，计时step调用未改。

公开[补丁](../tools/pure_linux/server_android/arm64emu-direct-guest-observer.patch)
以commit09b54a8的task bridge为基底，原SHA256为
`0fc7997c81b493e160fac5da64d8e6638194548b0978791ba2096964a2bfd5ff`。
先核对源SHA，再以`git apply --unidiff-zero`应用零上下文补丁。
它是本次测量适配，不是覆盖当前较新源码的默认补丁。三条转译器路线均用
同一个适配observer；不把未包含的五阶段identity观察算作本轮通过。
直接读坏指针会fault，没有伪装成通用安全读取器；arm64emu的其他状态
诊断仍为`HEADLESS_STATE_READ_UNAVAILABLE`，与对局归档校验分别报告。

另保留tonistiigi/binfmt QEMU10.2.3两次启动失败：显式linker参数路径错误、
调整入口后RuntimeInit注册失败。此分发有容器补丁，不推广为全部新版QEMU
兼容性结论；随后原上游构建已完整运行。源码解压首次因ROM绝对符号链接
被data_filter拒绝，user-only重试排除整个roms目录，继续保留路径校验。

## 内存、批次和环境边界

表中峰RSS来自进程getrusage；host250ms采样峰值分别为358.90、348.29、
476.73MiB。arm64emu的guest smaps RSS/PSS均返回-1，统计器输出null和
零有效样本，不把负值当内存数据，也不以RSS代替PSS。

从调用至退出的单批次耗时：旧QEMU132.47s、上游163.60s、arm64emu215.83s；
arm64emu20局，其余30局，且仅一次进程批次，因此不作为冷启动统计。
Android对照AVD在arm64emu测量期间暂停，finally已发送SIGCONT；随后
进程消失，原日志保留且原因未归因。两条QEMU测量期间该AVD确实未运行；
全部测量结束后按原相同配置恢复，ADB确认sys.boot_completed=1。

合成探针的double中位从旧QEMU1324.33ms降至arm64emu364.85ms，
约快3.63倍；整数却从174.42ms变成330.84ms。真实对局整体更慢，
再次说明单一算子快不等于游戏快。合成探针是同源另一个可执行文件，
动态链接转译器构建身份与正式静态构建分别保存，不推导真实热点占比。

## 工程检查和结论

纯Linux最终回归1176 passed、1 skipped，统计器14项测试通过。首次
系统python3缺cryptography导致16失败，保留日志；改用已有Anaconda解释器
复跑通过，没有通过安装或跳过来掩盖。此前公开9组1104 passed/42 skipped、
主工程211 passed、NDK严格构建和Java8/D8通过，沿用本轮先前结果。

当前继续使用原QEMU8.2.2更合适。后续若优化服务器路线，应先定位真实
step热点，再决定调整转译器配置或部署；本次没有足够证据提出盲目的替换。
完整headless目标、其他fixture、原wrapper、并发密度和冷启动统计继续未验收。

机器摘要：[alternative-translator-evidence-20261030.json](alternative-translator-evidence-20261030.json)。
公开只包含自有源码补丁、报告及脱敏摘要，第三方输入和转译器二进制留在私有目录。
