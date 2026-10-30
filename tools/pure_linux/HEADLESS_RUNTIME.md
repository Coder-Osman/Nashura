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

## 实验性 native 优化

`--native-optimization` 每次只选择一个候选，默认关闭；多个候选不叠加。
本机 ARM64 Linux guest 没有 QEMU 指令翻译层；本节与历史 x86 服务器的 QEMU
复测分开。Linux guest 仍承载现有 ELF/Bionic/ART 依赖，不是 macOS 原生移植。

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
