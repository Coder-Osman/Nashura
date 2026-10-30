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
