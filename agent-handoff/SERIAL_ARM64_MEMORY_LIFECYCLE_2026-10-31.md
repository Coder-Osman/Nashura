# ARM64 warm 引擎的生命周期内存诊断（2026-10-31）

状态：四输入33024局独立串行诊断、8完整局全帧门及主机原始证据复算通过；整体性能目标继续。
本轮没有新速度收益或无泄漏结论，候选继续默认关闭。

## 范围与方法

固定 Fusion ARM64 Ubuntu 2 vCPU / 6144 MiB、affinity [0,1]，VMX 与 guest 分别核对。
同 libg、资源、DEX、两卡组及 seed1/7、固定非空动作。新 observer 只加入默认关闭的
NASHURA_MEMORY_LIFECYCLE_EVERY；五边界为 pre_create / pre_step / post_step /
post_terminal / post_destroy。post_terminal 已销毁终态 serializer 临时对象，
post_destroy 已执行两个原始析构、GameState delete 和 replay JSON release。

每进程 16 task × 516 局，间隔128局采集，序号0..8192，共65个采样局/325个边界。
采集进程全局 mallinfo 四字段和六组 smaps（ART命名、allocator命名、stack命名、
其余匿名、文件命名、其余特殊）。按路径分类不等于对象归属；ART命名还包括线程栈、
JIT与表。匿名私有文件页可能是COW，不从类别名推断实际对象类型。

诊断与正式计时独立，所有本轮耗时排除速度比较。采样只在生命周期边界，step内
没有新调用。没有修改allocator、GC、VM或系统配置，不调用purge/trim，不重建外围
Android服务。只读外部ART映射快照也属于同一诊断阶段。

NDK 27.3 malloc.h 明确提示 mallinfo 不可靠；它是allocator特定进程全局快照，
不是分配次数、free次数或精确活对象身份。Android 11 bionic malloc_common.cpp
经GetDispatchTable/Malloc分派，未据此猜测当前构建的allocator类型。主要解释
来自同边界的反复观察与smaps变化，不把单次计数差当泄漏证明。

## 验证边界

新observer默认关闭采样时，四输入original/candidate共8完整局22698帧与历史参考的
每帧serializer payload及已观测状态一致。持续诊断每局校验终态digest/bytes，
并不代表持续局的每帧或全部未序列化内部状态已验证。

runner显式记录memory-lifecycle-diagnostic-only，拒绝与阶段、线程资源及动作录制
诊断混用。正式benchmark/stability汇总拒绝任何生命周期记录。独立汇总验证唯一配置、
完整序号/五阶段/六组、原始终态与task、采样时钟和相对step边界；主机图验收还核对
固定输入、安装标记、settings、plan、exit、压缩证据、环境、进程串行顺序与全帧门。
合成拒绝测试不替代真实引擎验收。

## 真实观察与解释

每进程8256局，四进程合计33024局；1300个生命周期边界、7800组映射分类。
首至末post_destroy采样覆盖a1/a7/b1/b7约226.077/243.624/112.354/107.680秒；
这些是含采样和构造的诊断区间，不是warm-step性能数据。

热身后筛选规则为sequence>=516，实际首/末640/8192，每组60个采样局。

| case | 热身后销毁后uordblks最小–最大 bytes | 首末差 bytes | ART命名RSS/PSS增长 KiB | 其余五组RSS/PSS增长 KiB |
|---|---:|---:|---:|---:|
| a1 | 127732608–127732608 | 0 | +20/+20 | 0/0 |
| a7 | 127734464–127734592 | 0 | +16/+16 | 0/0 |
| b1 | 127719312–127719440 | 0 | +20/+20 | 0/0 |
| b7 | 127717952–127717952 | 0 | +16/+16 | 0/0 |

同样筛选下，五个边界的uordblks首末差均为0；a7/b1有128 bytes瞬时浮动。
allocator命名映射RSS/PSS稳定。首次采样至末次post_destroy的uordblks差为
-96/-96/+32/-224 bytes，与热身后趋势分开。首局pre_create到pre_step/post_step
有加载、容量建立及页驻留变化，不将初期增长外推成每局泄漏。销毁边界uordblks
下降约58–68万bytes是进程全局净值变化，不是精确free字节数。

另在a7同一进程只读抓取两次ART命名smaps，相隔45.376秒：映射头集合完全相同，
唯一变化是[anon:dalvik-main space (region space)]的Rss/Pss/Anonymous/Private_Dirty/
Referenced各+4KiB。原始Java每task BEGIN/END及状态标记存在字符串拼接，Java任务
边界分配是合理候选，但尚无活对象/保留链证据，未把具体字符串判定为泄漏根因。
这个观察也不直接证明上一轮60–152KiB全部来自相同机制。

当前证据支持：在本轮有界诊断中，热身后的新增驻留集中于ART命名映射，未观察到
GameState销毁后native堆的持续累计。没有证据要求扩展system_server、图形或其他
外围Android服务；不为16–20KiB增长修改GC/allocator策略，不凭RSS主动释放引擎对象。
后续优先回到已有perf证据的空间查询、对象/组件更新及固定动作apply路径。

## 归档与工程检查

主机独立核验92个实验文件、336个部署工具文件，复算全部顺序/终态/映射统计与
8完整局22698帧。部署后仅主机合成fixture helper扩展和新验收器/测试加入；实际
运行的C、runner和内存汇总与归档部署源码一致。

归档6417840字节，SHA 5b5372ff73ea667f58d3d5be34a2ec98810d0fecdf80fc50aaa62f3798d31e19。
observer SHA 58c38e6369db5d336adc90d27d0a90e7f8215644057b4429a2cf9d20d7e9f274。
工具tar2385920字节，SHA 751d98ad16c8b5b0e06dc90f2a32332b55b3ec033b322a0b1024780ea8eee606。

pure_linux1503passed/1skip、公开1104passed/42skip、主工程211passed；历史持续
实验132096局/8全帧门用当前代码重新复算，所有派生字段与前次完全相同。凭据扫描
通过；源码清单、diff和暂存检查在发布前再执行。部署前1502项运行时工具回归通过；
stage-plan的engineering_checks_finished_before_launch只对应这套初始检查。后续新增
主机验收器、1503项回归及公开/主工程检查与独立诊断交叠；本轮没有任何正式速度
对照，不将诊断耗时用于优化收益报告。

公开只含自有代码、脱敏统计与身份；第三方ELF、完整资源及原始日志留本地。
机器摘要：serial-arm64-memory-lifecycle-evidence-20261031.json。

## 局限与后续

本轮只缩小内存增长的候选来源，不新增性能收益，不证明全局无泄漏。采样本身会
扰动驻留和allocator状态；smaps逐映射读取时其他runtime线程仍可能运行。
硬件PMU cycles/cache、精确off-CPU和更长稳定性仍需继续；不以软件线程时间冒充cycles。
候选默认关闭；最小headless、无并发、功能分支、main及冻结部署原样的边界保持。
