# 固定版本的无 Binder headless 入口

状态：本fixture完成原生终局及Android逐tick serializer配对；整体目标继续进行。

## 边界

headless_art_runtime.cpp复用原始AndroidRuntime的VM设置和native注册，直接进入
HeadlessRuntimeEntry，省去AppRuntime/RuntimeInit启动的Binder线程池。未知系统服务
由HeadlessApkContext显式报错。原始APK、DEX、libg、ART及框架闭包由操作者本地持有。
没有启动system_server、原始init、SurfaceFlinger、Display或GPU/EGL服务；libEGL等
DT_NEEDED库仍可能被加载，不等于已证明这些库或其所有函数与战斗无关。

run_headless_namespace.sh在私有user/mount/network/IPC namespace使用新/dev和/sys，
仅暴露null/zero/random/urandom，屏蔽oldroot/sys，保留只读本地property数据。
闭包不是安全沙箱：oldroot、proc和其他宿主路径仍可见。guest全局历史policy未改变，
因此不宣称全局policy独立；本次只是减少进程可见的设备和SELinuxfs挂载。

## 构建

使用Android30 ARM64 NDK clang++及原始运行库。headless_art_build_inputs.json固定
android-11.0.0_r1官方sigchain源文件与22项源码/头文件的URL/SHA256。将源码下载到
私有$AOSP目录并逐项校验SHA；保持sigchain.cc、sigchain.h、log.h和include目录结构。
第三方源和二进制不随本仓库复制。构建目录必须新建。

```sh
"$CLANGXX" -std=c++17 -static-libstdc++ -fno-rtti -fno-exceptions -fPIE -pie \
  -g -O2 -Wall -Wextra -Werror -Wl,-z,defs -Wl,--export-dynamic \
  -Wl,--exclude-libs,ALL -I "$AOSP/include" \
  tools/pure_linux/headless_art_runtime.cpp "$AOSP/sigchain.cc" \
  -o "$OUT/headless-art-runtime" -L "$CLOSURE/system/lib64" \
  -Wl,-rpath-link,"$CLOSURE/system/lib64" -landroid_runtime -lutils -llog -lsigchain
```

sigchain.cc必须使用真实实现并导出信号链API；默认系统libsigchain是跳转入口，
单独链接会在VM启动时中止。NDK C++支持静态链接并隐藏归档符号，避免与系统
libc++命名空间相混。AndroidRuntime对象大小0x48及虚方法顺序固定为当前ARM64 ABI；
runner将libart/libandroid_runtime/libutils身份固定，其他构建须重新审计。

Java8/D8探针构建在生命周期报告的既有类之外加入HeadlessRuntimeEntry.java。
observer同时编译art_headless_observer.c、art_headless_task_bridge.c和headless_call_arm64.S。
将新可执行文件放入闭包apex/com.android.art/bin，沿现有ART linker分区加载；
任意宿主目录会落入其他linker分区并可能缺原有APEX依赖。

## guest本地复验

磁盘有限时可显式使用 --compress-stdout。引擎仍直接写普通文件，进程退出并
记录退出码后才 gzip 压缩，重新解压计算 SHA 与原文件一致后回收 raw 副本。
压缩失败保留原始日志，非零引擎退出仍保留 gzip 证据并拒绝验收；默认输出不变。
该选项不会在 warm step 期间启动压缩管道，不删除历史归档或未验证证据。

```sh
python3 tools/pure_linux/run_headless_match.py "$SETTINGS" --output "$NEW_OUTPUT"
python3 tools/pure_linux/run_headless_match.py "$SETTINGS" --output "$NEW_OUTPUT" --apply
```

默认只校验输入并输出计划。settings JSON含closure、namespace、linker_config、runtime、
probe_dex、observer、apk、replay八个绝对规范路径，以及identities字典：每个绝对输入
路径映射64位小写SHA256。路径不含凭据。必须固定所有required输入与本地property
文件；操作者还应登记完整第三方闭包及APK依赖库，runner当前不追踪所有传递加载。
当前probe内部DEX路径固定为/data/local/tmp/current-game-classes.dex，精确游戏库路径为
/data/local/tmp/libg-input/libg.so，VM观察库为/data/local/tmp/libart_engine_jni_probe.so。

runner不继承环境中的服务启动开关，最多45秒加3秒终止窗口；新证据目录保存计划、
stdout、stderr、真实退出码、逐tick归档及结果。完整终局、Java正常返回和PM零拒绝
三者同时通过才产生结果。归档连续tick、帧长和终点都校验；零退出或孤立函数返回
不作为战斗完成。与Android对照仍用compare_headless_snapshots.py独立比较。

阶段计时默认关闭，设置`NASHURA_PHASE_TIMING=1`后才进入guest计划；
`NASHURA_BATTLE_OBSERVATIONS=1`关闭quiet模式并输出关键tick对象/队列状态。
逐tick snapshot trace 默认保持开启；路径诊断可传`--without-snapshot-trace`，此时
结果只声明原生终局已观察，不声明产生或校验了归档。trace关闭不会放宽终局、Java
正常返回或PM零拒绝条件。诊断开关不改变service依赖边界。

`--benchmark-repeats`启用最小 step 计时并关闭逐tick trace。每局按
`--step-sample-every`抽样；重复局的起始相位确定性轮转，并在 metrics 行记录
`sample_phase`/`sample_phase_period`。汇总器核对相位次序、样本数和非空动作序列，
避免固定 replay 在每次 reset 中只采到相同的 tick 模相位。计时数据仍只覆盖 warm
step loop；不同 observer 构建或计时实现的结果不应直接作前后性能对照。

重复局在同一个 Mainloop task 内执行。benchmark runner 将
`NASHURA_TASK_TIMEOUT_MS` 设为 `min(--timeout-seconds * 1000, 120000)`，
避免较长的串行批次触发原生默认约五秒等待；外层进程 timeout 仍独立生效，
不保证一定等到 task 的完整上限。此设置只改变控制线程的等待契约，不改变 step。

## 独立线程资源诊断与有界串行稳定性

`--thread-resources --benchmark-repeats 216` 显式启用独立诊断，默认关闭。
仅在 warm loop 两端读取线程 CPU 时钟与 RUSAGE_THREAD，逐 step 不新增系统调用；
输出包围 loop 的边界、user/system、缺页、上下文切换与块 I/O 计数。
summarize_headless_resources.py 要求计数非负、边界有序且与每局计时及终态一致。
边界含测量成本，线程 CPU 不是硬件 cycles，wall-minus-CPU 不解释为锁或 Binder
等待；资源诊断与安静基线分开运行，不直接用于加速对照。

`--serial-batches 10 --benchmark-repeats 516 --timeout-seconds 600` 在同一个
已初始化引擎进程内串行提交十个 Mainloop task。默认仍只有一个 task；显式多批
限制为 2..240，每批 repeats 仍为 2..1000，task 等待仍不超过 120 秒。
多批禁止混入 native/线程资源诊断、手牌记录及逐 tick trace。每个 task 记录
BEGIN/END，全部局仍校验正常原生终态；summarize_headless_stability.py 进一步
核对唯一合法 create 返回、同一 Mainloop TID、批次顺序、时钟和正常进程完成。

稳定性汇总按每批剔除 warmup，报告实际观察时长、RSS 和终态身份。连续 task
共享进程与固定输入，不当作独立实验，不从终态 digest 外推全部 serializer 帧或
未序列化状态，也不将有限运行外推为全天或无限期稳定性。正式速度测量继续关闭
上述诊断，使用独立交错对照；固定 original→hot 顺序只用于稳定性观察。

组合候选另以八个新完整局、32 批 ABBA/BAAB 和四进程各 24 task × 516 局
独立复测；主机工程检查先结束，再启动 guest，原始归档取回后由
verify_combined_followup.py 核对精确计划、安装标记、固定 replay/动作计数、
结果字段、全帧、终态和 Mainloop 身份。每进程实际观察 163.97–374.45 秒，
不外推全天或无泄漏；详见[报告](../../agent-handoff/SERIAL_ARM64_COMBINED_FOLLOWUP_2026-10-31.md)。

## 冻结多 seed / 卡组动作

`--record-hand-actions --without-snapshot-trace` 仅用于独立诊断运行，要求输入
`cmd`、`inject`、`evt` 全部为空，与 `--benchmark-repeats` 互斥。沿用原始 selection
builder、parse 和 append；每 100 tick 尝试两方首张手牌，保留引擎产生的 packed
selection。该模式开启关键 tick 状态观察，不进入性能计时。

```sh
python3 tools/pure_linux/run_headless_match.py "$RECORD_SETTINGS" \
  --output "$RECORD_OUTPUT" --record-hand-actions --without-snapshot-trace --apply
python3 tools/pure_linux/freeze_headless_actions.py "$EMPTY_REPLAY" "$RECORD_OUTPUT" \
  --output "$NEW_FIXED_REPLAY" --summary "$NEW_ACTION_SUMMARY"
```

冻结器校验源 replay SHA、计划、正常退出、原生终局、两方非空动作、slot/card/packed、
成本及入队增长，原样保留动作 JSON。它只证明 selection 与 queue acceptance。之后
为固定 replay 更新 settings 路径及 SHA，以相同固定输入独立运行两次，并比较全部
原始 serializer 帧和已观察的状态；动态入队与预加载命令的中间 queue 不要求相同。
固定 fixture 位于 `fixtures/headless-runtime-deck-{a,b}-seed-{1,7}.json`，它们覆盖两个
实际不同的八卡集合，不代表全部卡牌、玩法或未序列化全局的等价性。

## 独立 native 阶段诊断

--native-phases 默认关闭，只用于固定 replay 的独立诊断，禁止与 benchmark、
手牌记录或任意 native 优化同时启用。十一个精确原始调用点经 guard 后进入包装器，
仍调用原目标，保留 BL 和尾跳 B；进程内 RW→RX，不修改原始 libg 文件。
从 maps 的空隙提供近分支岛 mmap 提示，验证实际返回地址，不覆盖已有映射；
地址空间布局不满足时拒绝任务，不修改 guest 系统配置。

九窗口覆盖 state/world/object collection、command queue、mode 2/3 dispatch，
以及对象临时 new[]/delete[] 和组件虚函数更新；
统计 inclusive/exclusive、原始返回值和零时长次数，exclusive 只扣已插桩子窗口。
新 schema v2 记录请求分配字节，并校验对象嵌套和原 allocator 目标；旧六窗口 /
八调用点日志继续输出原 schema v1，支持历史归档复算。诊断含时钟和计数开销。
零时长代表时钟量化，零返回代表原始状态，两者均不等于免费或动作成功。
runner 验证正常原生终局及队列/计数，写 native-phases.json。

四组输入的原始、cmd 诊断和 inject 诊断全部全帧及已观测状态与历史一致。诊断
包含计时与计数开销，不用于优化收益比较；解析/入队在 step 前，也不当作在线
动作延迟。mode 2 未覆盖，业务语义、未序列化状态及其他 fixture 不外推。详见
[阶段报告](../../agent-handoff/SERIAL_ARM64_NATIVE_PHASES_2026-10-31.md)。

## 实验性 native 优化

`--native-optimization` 每次选择一个显式候选，默认关闭；组合仅通过下述
`hot-distance-v1` 入口启用，不能任意叠加其他候选。
本机 ARM64 Linux guest 没有 QEMU 指令翻译层；本节与历史 x86 服务器的 QEMU
复测分开。Linux guest 仍承载现有 ELF/Bionic/ART 依赖，不是 macOS 原生移植。

### 饱和平方距离

--native-optimization saturated-distance-v1 在精确原始字节 guard 后替换两个
固定函数体，默认关闭、与其他候选互斥。保留 32 位坐标差回绕，使用有符号扩宽
乘法与无符号 64 位和比较实现 INT32_MAX 饱和；指针版保持首坐标越界时不读取
第二坐标。仅改变普通调用边界允许的临时寄存器/flags，未承诺故障 PC、信号或
profiler 栈一致。仅在 Mainloop 首局前安装，文件不变、RW/RX 分离并刷新缓存。

check_saturated_distance.c 用现有 ARM64/Bionic namespace 执行原始机器码与候选，
验证边界/随机输入和 PROT_NONE guard page。真实多 seed/卡组完整局全帧门仍
独立执行，模型与指令对照不代替引擎等价。verify_saturated_distance_evidence.py
接收解包归档位置、--reference 上阶段原始目录及 --output 新验证 JSON，独立
核对全部原始日志、计划、身份、全帧门与 ABBA/BAAB 批次。

原始 evidence 由操作者本地持有；公开摘要只含脱敏计数和 SHA。候选收益、批次
顺序和统计边界见[阶段报告](../../agent-handoff/SERIAL_ARM64_SATURATED_DISTANCE_2026-10-31.md)。

### 旧 getter 与饱和距离的显式组合

`--native-optimization hot-distance-v1` 顺序调用旧 11 点 getter 与两个饱和距离
安装器，保持各自原始字节 guard、RW/RX 分离与缓存刷新；两组写入范围不重叠。
子安装失败即中止一次性进程，安装完成前不进入首局。三个安装标记必须精确匹配
getter、距离、组合的顺序，各出现一次；缺失、重复或额外标记均拒绝验收。
默认关闭，不包含 52 点组件扩展或空间候选，普通调用语义及固定构建边界沿用子候选。

`summarize_headless_fourway.py ROOT --reference REFERENCE --output NEW_JSON` 从
操作者本地原始归档独立复算四路径：a 原始、b 旧 getter、c 距离、d 两者组合。
ROOT 的相邻 metadata 目录必须保存控制进程正常退出证据。16 个完整局门逐一
解压 gzip serializer 归档，核对每帧、已观测状态与历史原始局；64 批按 abdc、
bcad、cdba、dacb 顺序，每批 16 warmup + 100 measured。验证精确命令、环境、
输入身份、终态、全部安装标记和保存摘要后，以四个批次为比较单位。报告分别给出
组合相对原始及两单项的结果；不把历史百分比相加，终态门也不代替全帧门。

### 空间查询边界

`--native-optimization spatial-y-bound-v1` 显式启用固定版本候选，默认仍调用原始代码。
同一 observer 可测两个路径。Java 固定 libg SHA 后，Mainloop 在首个 battle 前额外
校验入口、原始外层循环与待替换指令字节，再仅修改进程内 `+0xf34c84` 的 `b.le`
目标，从内层 y 增量改为原始外层增量 `+0xf34c38`。文件保持原始字节；页权限先
RW、后 RX，并刷新指令缓存；安装后该进程内保持启用至退出。runner 要求唯一的
成功安装证据，旧 observer 即使正常完成对局也不视为优化成功。

候选只跳过 `y >= height` 后剩余越界 y，保留原始候选顺序、过滤、输出及清理。
适用边界为当前固定 ARM64 build、串行 Mainloop 和查询期间稳定的 grid dimensions；
它不保证其他构建或并发修改的语义。完整局 serializer 配对证明已覆盖 fixture 的
序列化状态一致，工程模型测试只检查跳转编码和跳过单元格的次序。性能收益必须
另由相同 observer、相同输入、关闭诊断的多批 A/B 对照证明，不能由该模型推断。

### 热 getter 调用内联

`--native-optimization hot-getters-v1` 在三个热调用者的 11 个调用点，以 getter
原有单条字段 LDR 替换 BL。四个 getter 均严格为 LDR 加 RET，读取时机、宽度、
偏移及顺序不变。安装器校验调用者入口、原始 BL 与 getter 的 LDR/RET，全部通过
才按 RW → 写入 → 指令缓存刷新 → RX 修改一次性进程；原始库文件保持不变。
调用者保存并恢复 LR，正文不依赖中间 LR；正常执行语义之外的 fault PC 和
signal/profiler 调用栈不在等价范围。非简单 getter 不参与此候选。

四个固定多卡组/seed 完整局需分别通过原始与优化 serializer 全帧和已观测状态
对照，再进行同 observer 的串行 A/B 批次计时；终态 digest 一致本身不是全帧门。
详见 `agent-handoff/SERIAL_ARM64_HOT_GETTERS_2026-10-31.md`。该候选默认关闭。

### 组件 getter 扩展与三路径比较

`--native-optimization component-getters-v1` 包含旧 11 点及两个已采样组件调用者的
41 个额外 BL→原始 LDR，共 52 点，继续默认关闭。全部点位、getter LDR/RET、
调用者入口与 LR 使用已核对，适用边界同旧 getter 候选，不承诺异常 PC/信号栈相同。
四组原始/旧/新候选与对象诊断共 16 局全帧和已观测状态一致。

`summarize_headless_threeway.py ROOT --correctness CORRECT_ROOT --output NEW_JSON`
独立复算六种三路径全排列、72 批原始日志，严格核对计划、环境、终态、保存摘要和
CPU 配额。每批 16 warmup + 100 measured；统计单位为 batch，不把局数视为独立样本。
新相对旧候选四组变化有正有负，没有跨 fixture 稳定增量收益，保持 opt-in。临时
分配/释放诊断占对象窗口不足 1%，本轮不增加缓冲池。详见
[阶段报告](../../agent-handoff/SERIAL_ARM64_COMPONENT_GETTERS_2026-10-31.md)。

## 只归因 warm step 的 perf 采样

在 ARM64 guest 内以 `cpu-clock:u` 采样，显式使用 `--clockid mono`。如需 sudo，
仅让 perf 获得所需权限，通过 `runuser` / `setpriv` 将 runner 和引擎降回普通 guest
用户。不要把整个 profile 的 Python 校验、ART 初始化或 reset 样本混入 warm step。
以下命令中的路径由操作者提供；第三方输入仍须通过 runner 的身份校验：

```sh
sudo perf record --clockid mono -F 997 -e cpu-clock:u -g --call-graph fp \
  -o "$NEW_DIR/perf.data" -- runuser -u "$GUEST_USER" -- \
  python3 "$RUNNER" "$SETTINGS" --output "$NEW_DIR/run" --apply \
  --benchmark-repeats 216 --step-sample-every 16 --timeout-seconds 120
perf script --ns -i "$NEW_DIR/perf.data" -F comm,pid,tid,time,event,ip,sym,dso > "$NEW_DIR/perf-script.txt"
perf script --ns --show-mmap-events -i "$NEW_DIR/perf.data" > "$NEW_DIR/perf-maps.txt"
perf evlist -v -i "$NEW_DIR/perf.data" > "$NEW_DIR/perf-evlist.txt"
perf report --stdio --no-children -i "$NEW_DIR/perf.data" > "$NEW_DIR/perf-report.txt"
```

执行前新建 `$NEW_DIR`、确保普通 guest 用户对输出目录有权限；分别保留 perf、runner
及外层 wrapper 的真实退出码，不用管道或最后一条 `perf report` 的成功覆盖前序失败。
必要的输出所有权交接在 workload 退出后完成。`summarize_headless_perf.py --help`
列出汇总输入：runner log/plan、script/maps、evlist/report 和三个退出码文件。
使用 `--repeats 216 --warmup-matches 16` 只保留后 200 局的 `[start_ns,end_ns)`
step 窗口；汇总器复用完整终局/serializer 校验，要求零丢样、唯一 Mainloop 进程、
明确的 monotonic clockid、精确的原生引擎身份及最小观测计划。

归一化地址是 `IP - MMAP2.start + MMAP2.pgoff` 的 ELF 文件偏移，需核对精确 ELF 的
PT_LOAD 后再解释为虚拟地址/函数范围；frame-pointer callchain 中的 return PC 也不是
采样 IP。样本比例只用于热点优先级，不是函数耗时、cache miss、cycles 或优化收益。
速度基线另用同一构建、不带 profiler 的串行运行测量。

verify_combined_profile.py ROOT --correctness FULL_FRAME_ROOT --engine EXACT_ELF
--output NEW_JSON 复核四组组合路径的采样、精确外层命令、uid/gid 1000 workload、
安装标记、固定动作数、退出和全帧参考。精确 ELF 的 GNU_EH_FRAME 索引与 FDE
共同确定已观察热点范围；固定编码不符即拒绝。替换后的距离及 getter 调用点
按进程补丁身份解释，原始函数字节 SHA 仅识别 build。此工具输出归因，不输出
优化速度收益；详见[报告](../../agent-handoff/SERIAL_ARM64_COMBINED_PROFILE_2026-10-31.md)。

## 已验证与未闭合

v52直接入口exit0，strace零Binder设备打开及零Binder ioctl；v53/v54使用仓库runner，
其中v54额外屏蔽oldroot/sys。三者都到tick3601，3602帧、12,334,754字节，SHA256：
e5bc727424104a14059519ae097bccc6389137cbd1ae97397abea9eea40da206。

快照是显式原始serializer适配，原始客户端wrapper的controller空缺仍保留。
单fixture、其他卡组/模式、未序列化全局、ART逐库最小化、ResourceManager完整初始化
身份与历史strict catalog门都不是这些结果自动闭合的范围。带逐tick快照和strace
的对局不纳入之前性能表；新启动器的性能尚未重测。

## x86服务器与内容对象图补充

服务器QEMU版本用-DNASHURA_QEMU_PTHREAD_ENTRY选择真实pthread入口，
ART主线程直接读取host栈映射导致的mprotect失败不通过伪造返回值处理。
该服务器在固定fixture完成独立归档及30局正式测速；不是本机旧入口性能重测。
Android native bridge专用诊断开关只跳过自有JNI表hook及信号采样。
实际配置、适用范围与复验见server_android/README.md及服务器报告。

v56identity在ARM guest直接入口观察bootstrap-after、catalog-before/after、
battle-before/after五阶段的root/context/wrapper/singleton关系、vtable和
真实getter，均verified=1且跨阶段稳定；原生终局和3602帧归档仍通过。
这只是版本绑定对象图身份，完整ResourceManager及owner初始化语义继续待验。

本轮 replay 路径诊断中，原始52条`battle.cmd`与runner显式注入的52条`cmd`均到
tick3601，终态摘要一致；队列从52降至2，历史v38的730条状态记录逐行相同。
阶段耗时仅为单次诊断观测；没有并发测试、吞吐测量或snapshot trace归档。详见
`agent-handoff/HEADLESS_REPLAY_ROUTE_DIAGNOSTICS_2026-10-30.md`。

## 网格清理展开候选（2026-10-31）

grid-clear-unroll-v1 只替换精确 build 的 +0xf34864..+0xf348d4；保留逐 pair 读写顺序、模乘和 signed 边界。hot-distance-grid-v1 显式组合既有 hot-distance-v1，要求五个有序安装标记。24107 组真实指令、12 个完整局门及 32 批无 profiler 对照通过，但增益不一致，仍默认关闭。详见 ../../agent-handoff/SERIAL_ARM64_GRID_CLEAR_UNROLL_2026-10-31.md。
