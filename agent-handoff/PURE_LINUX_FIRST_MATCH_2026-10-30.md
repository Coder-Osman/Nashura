# 真实 ARM64 Linux 首局与快照适配边界

日期：2026-10-30。分支：`codex/pure-linux-libg-headless-20261026`。
状态：`NATIVE_FULL_MATCH_OBSERVED_LIMITED`；完整目标继续进行。

## 真实运行结果

- v36 连续500步退出0，但原 fixture 的 `sp=[]`，`sc` 中159000000为塔兵，
  不是可部署八卡组。双方手牌全为-1，原命令消费不代表 apply。
- v37 按既有 MatchConfig 格式向 deck.sp 填入真实八卡组；保留 deck.sc 塔兵。
  通过原始0xe2d1bc生成selection.packed，读取真实玩家身份，经过原始JSON parser、
  queue append与step。未强制执行器返回成功，未修改费用、手牌或塔血。
- v37 连续1000步可见扣费、手牌轮换、单位生成、移动与伤害。
- v37full 双方累计入队52条原生命令，在tick3601由引擎写出finalized=1、
  result_raw=1，退出0。此前出现公主塔消失、主塔血量2400降至180。
  终局时仍有2条尚未到执行时刻的命令，不将52条入队解释为52条全部apply。
- v38replay 将这52条固定命令放在root.cmd，完全由原始loadReplay装载，
  关闭主动部署。再次在tick3601终局。661条已采样玩家/对象字段逐行相同，
  该比较不是逐tick完整状态或Android等价性验收。

## 快照真实边界与最小适配

完整bootclasspath下，v38snapshot确实进入战斗加载后，在原始snapshot wrapper
0xad9a50路径SIGSEGV，fault address=0x48。精确反汇编显示wrapper调用
0xc85c28 / 0xc89c10；后者经0xc8c5b8取得客户端controller，再读其上下文。
后续探针实测controller为NULL。此证据支持处理该边界，不支持重建Android服务。

显式NASHURA_SNAPSHOT_ADAPTER模式仅复现原wrapper的ByteStream前导，
调用原始0x10632b0 header和0x1062c3c body serializer；缺失的客户端context
保持NULL，不创建替身controller，不设置ready。同版本Android配对已通过，
该结果仅覆盖下文限定的fixture及serializer状态，仍属显式诊断适配。

v39在tick0得到3418字节，推进100步后restore回tick0，重新序列化字节摘要相同。
v40进一步在真实交战tick900保存3889字节，运行到tick3601；restore回900后
续演至3601，3091字节终态摘要均为6c959326dd2f09cc。同一Mainloop中销毁并
重新构造/装载GameState两次，恢复续演与终态摘要都相同。这里reset特指完整
销毁/重建流程，不宣称已验证其他原生reset入口。摘要使用探针现有64位算法，
不是加密完整性或跨平台字节等价证明。

## 历史纠正

- v35h1把append后临时owner指针仍非空视作错误；精确原始正常入队分支不清零
  该临时指针，已改为检查队列增长，历史失败保留。
- v35h2的root.cmd已被loadReplay装载，再主动append造成重复命令；
  其queue1→2与单次step证据不属于干净动作复演。v35h3起使用独立inject，
  v36起明确区分loaded_cmd与inject_cmd。
- v35h2 snapshot runner漏core-icu4j.jar，在ART启动阶段134退出；
  此前将其描述为snapshot内部崩溃的解释撤回。v38snapshot才是真正wrapper路径失败。

## 最小依赖与未完成项

本组Linux运行沿普通user namespace、真实Bionic/ART/APK DEX与精确libg，
未启动system_server、Display、SurfaceFlinger、GPU/EGL或原始init。
既有ART/框架闭包尚未逐库最小化；guest仍保留历史permissive policy。
APK局部Context、固定Resources和本地Intent边界沿生命周期报告；不承诺完整框架等价。
HEADLESS_BATTLE_READY=UNVERIFIED保留；catalog表数162、23868行是结构观测，
不自动升级历史严格catalog全门。

原始snapshot wrapper的客户端controller仍为空；配对使用同一显式serializer adapter。
Android完整运行时的无窗口进程基线已运行；该AVD自身有系统/图形服务，不将它们
反推为Linux战斗必需。广告类缺失和既有Java回调空指针日志保留；本组生命周期
与战斗能完成，不代表相关客户端功能可用。完整目标尚未达到。

机器证据：pure-linux-first-match-evidence-20261030.json。
第三方APK、DEX、ELF及原始strace仅保留私有证据，不随公开仓库发布。

## 同版本 Android 配对与逐 tick 字节

只读API30 ARM64 AVD，使用相同APK、DEX、libg、八卡组、seed、52条命令及
同一observer。首次缺libscid_sdk.so的失败保留；补入APK原始DT_NEEDED库后
Android退出0。两次恢复/重建与Linux的1328条观测逐行一致。
逐tick采集0..3601，共3602个原始serializer payload；归档均为12,334,754字节，
SHA256均为e5bc727424104a14059519ae097bccc6389137cbd1ae97397abea9eea40da206。
严格归档比较器检查连续tick、长度、终点和尾部；逐字节一致。覆盖该fixture及
serializer包含的状态，不证明未序列化全局、其他卡组/模式或原始客户端wrapper等价。

## 性能与并发密度（限定测量）

移除strace和逐tick快照，保留每局真实finalized与测量窗外终态摘要检查。
两平台各30局均到3601，终态3091字节摘要一致。Linux / Android：
中位对局step循环45.421 / 45.685毫秒；总step吞吐79,075 / 78,603 tick/s；
单step p50中位8.604 / 8.605微秒，p95为29.385 / 29.167微秒，
p99为53.431 / 55.229微秒；进程峰值RSS260.45 / 262.85 MiB。
这里不是进程冷启动、资源初始化或端到端RPC延迟。

每平台在1/2/4并发各运行100/200/400局，共700局，全部原生终局和摘要通过。
Linux聚合吞吐75,136 / 151,833 / 142,441 tick/s；Android为
75,759 / 149,056 / 286,396 tick/s。Linux只有2vCPU/6GiB，Android AVD为
6vCPU/8GiB，4进程的差异受CPU配置限制，不作平台固有优势结论。
各进程观察到的峰值PSS求和，Linux为258.39 / 399.18 / 676.72 MiB；Android为
178.57 / 337.57 / 651.26 MiB。峰值求和非同一时刻全机内存，且Linux/Android
共享库驻留和系统进程分摊不同，不作整机密度或总VM内存优势结论。
首轮Linux密度runner误改fixture路径，7个进程137退出；旧证据保留，v44修正路径。

## 复验入口

文件tools/pure_linux/fixtures/headless-eight-card-replay.json保存本轮合成配置及固定
命令，全部输入来自既有MatchConfig与原始selection输出，不含第三方资源。
构建observer须同时链接art_headless_observer.c、art_headless_task_bridge.c、
headless_call_arm64.S，使用-Wl,-z,defs。NASHURA_BATTLE_STEPS控制步数；
NASHURA_SNAPSHOT_ADAPTER显式选择诊断serializer适配；NASHURA_SNAPSHOT_AT=900
用于交战中快照；NASHURA_RESET_REPEATS=2验证重建；NASHURA_TRACE_SNAPSHOTS
指向新建私有归档，O_EXCL拒绝覆盖；NASHURA_QUIET_SNAPSHOTS=1抑制逐tick输出。
性能模式使用NASHURA_QUIET=1与NASHURA_BENCHMARK_VALIDATE=1，关闭trace和strace。
完整headless运行仍依赖操作者持有的固定第三方APK/DEX/ELF与ART闭包。

## 最终源码复验与加载边界

v45使用最终源码构建（NDK -Wall -Wextra -Werror -Wl,-z,defs），两次
完整终局、tick900恢复续演及3091字节终态摘要均通过。Java8/D8重建DEX与
本轮既有运行DEX SHA相同。原始libg的DT_NEEDED包含libEGL等客户端共享库，
本轮仍使用它们完成链接；未主动创建EGL图形上下文或启动显示器、GPU服务、
SurfaceFlinger。尚未覆盖记录所有libEGL函数调用。共享库链接闭包不等于战斗调用必需，后续逐项裁剪须另给证据。

## 工程验证与发布范围

Java8/D8源码重建与NDK -Wall -Wextra -Werror -Wl,-z,defs通过，未遗留
cr_*自有未定义符号。纯Linux全集1150 passed、1 skipped；主工程211 passed；
公开9组共1104 passed、42 skipped，全部exit0。新增归档比较器10项测试在
纯Linux全集内通过。gitleaks无泄漏；历史publication XML字节保持原样，
本轮XML、日志与构建输入SHA保存在机器证据。阶段结果在当前codex分支发布，
main、冻结部署、第三方输入和原始失败证据保持原样。


## 设备裁剪复验（v46–v48）

新增独立run_headless_namespace.sh，移除旧入口中init、APEX激活、图形服务与HAL
分支。私有mount namespace中新建/dev和/sys，只暴露null/zero/random/urandom及
原始app_process启动/退出当前使用的binder、hwbinder；没有vndbinder和图形设备。
保留既有本地property区并以只读方式挂载，不启动property service或Android服务。

v48使用最终observer和固定52命令完成tick3601原生终局、exit0；归档仍为
12,334,754字节、3602帧，与Android基线逐字节一致，SHA256仍为
 e5bc727424104a14059519ae097bccc6389137cbd1ae97397abea9eea40da206。
该运行开启strace与逐tick快照，不纳入性能表。

v46同时移除property数据与Binder等设备，原生启动阶段exit0但未进入Java、没有
战斗或快照，明确判为未通过；不以零退出作成功。v47仅将Binder驱动替换为/dev/null，
原始app_process在Java探针前触发BINDER_VERSION/ENOTTY并SIGABRT(134)。
这证明当前启动器前置条件，不能据此证明战斗路径需要Binder或系统服务。
Android11原始app_main.cpp的AppRuntime::onStarted在callMain前启动ProcessState
线程池，并在退出后清理Binder/hwbinder，后续应处理启动器边界而非扩建服务。

边界：/sys/fs/selinux在根视图缺失，但libselinux仍可经/oldroot/sys/fs/selinux
发现历史guest挂载；不宣称policy无关或全局SELinux独立。ART/框架、DT_NEEDED
客户端库仍未逐库最小化。设备根视图裁剪结果只覆盖本fixture。


## 直接ART入口与零Binder复验（v49–v55）

新增headless_art_runtime.cpp，保留真实AndroidRuntime构造、VM启动和native注册，
直接进入HeadlessRuntimeEntry；省去AppRuntime/RuntimeInit在Java main前的Binder
线程池与退出清理。没有修改libg或构造假Binder成功。真实AOSP sigchain实现静态
编入可执行入口并导出信号链API；构建输入的官方URL与SHA在仓库单独固定。

v49因可执行文件位于宿主路径而落入错误linker分区，缺libstatssocket，exit1；
v50修正分区后因缺真实sigchain导出SIGABRT(134)。v51完成真实终局和字节配对，
但自定义入口错误要求不存在的probe退出标记，exit70按失败保留。v52在Java探针
完整正常返回后明确System.exit(0)，原生终局、正常收尾与退出0同时通过。
strace记录零Binder设备open和零Binder ioctl；同一3602帧归档与Android一致。

run_headless_match.py默认只做身份校验和计划输出，--apply在新目录运行完整replay，
保存退出码、日志、归档与结果。它固定游戏和三项运行库SHA，校验全部必需文件和
本地property区，拒绝继承服务启动开关。只有真实finalized、Java正常完成和PM零拒绝
才产生结果。v53真实运行该工具通过；v54额外屏蔽私有namespace内oldroot/sys仍通过。
最终v55复验采用最终C++源码和固定运行库身份校验。所有成功归档仍为相同SHA256。

当前默认namespace仅暴露四个基础设备，不挂载Binder、图形或SELinuxfs；oldroot/sys
已屏蔽。guest全局历史policy继续原样，proc与oldroot其他路径仍可见，不宣称安全
沙箱或全局policy独立。ART/框架/DT_NEEDED库与原始snapshot wrapper边界仍待处理。
本轮是正确性复验，新入口性能尚未重测。构建和guest本地复验详见
 tools/pure_linux/HEADLESS_RUNTIME.md。

纯Linux回归在已有完整Python环境1162 passed、1 skipped；新增runner/比较器22项
在其中。首轮默认Python缺cryptography造成16失败，日志另存；未通过安装绕过。
历史主工程211 passed与公开1104 passed/42 skipped保持其原运行范围，不冒作本轮重跑。
