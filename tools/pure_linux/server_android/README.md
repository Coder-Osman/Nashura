# x86服务器实验入口

替代转译器及原QEMU相同observer复测见
`agent-handoff/ALTERNATIVE_TRANSLATORS_2026-10-30.md`。
`arm64emu-direct-guest-observer.patch`仅用于报告指定09b54a8基底和源SHA；
应用前核对身份，在独立构建目录操作，勿覆盖当前较新的task bridge。
先核对源SHA，再以`git apply --unidiff-zero`应用零上下文补丁。
它保留真实guest读取和原检查，直接读坏指针仍会fault；不是通用诊断替代。
缺失guest smaps以null和零有效样本报告。

本目录仅包含自有Instrumentation和ARM JNI环境桥源码；第三方游戏及Android
运行库由操作者本地提供，不随仓库发布。测量结论见
`agent-handoff/X86_SERVER_HEADLESS_COMPARISON_2026-10-30.md`。

## Android native bridge

使用支持arm64-v8a的API30 Google APIs x86_64 r16镜像，
getprop ro.dalvik.vm.native.bridge须为libndk_translation.so。
仅有x86 ABI清单的镜像不满足本实验入口。镜像官方压缩包SHA1：
`6ae21030eaadc041078444d3798e4b399f3e787d`。

将本目录BenchRunner、HeadlessApkContext、ArtEngineLoadProbe、
ArtHeadlessCreateProbe和生成的UnsupportedHeadlessPackageManager以Java8编译。
先用D8合并自有classes及固定原始游戏DEX；不要重写原始输入DEX的SHA身份。
用NDK Android30 aarch64编译bench_env.c为libnashura_bench_env.so。
用aapt2 link提供本目录manifest，打包classes.dex及应用lib/arm64-v8a
下所需原始APK库，zipalign后以操作者临时测试密钥签名。
测试APK和密钥仅存私有目录，不发布。

安装时显式 `adb install --abi arm64-v8a BENCHMARK_APK`。
按BenchRunner约定将固定输入放入设备本地/data/local/tmp/nashura-x86，
包括game.apk、原始libg、原依赖库、observer、VM观察库及replay.json。
observer由同一art_headless_observer.c、art_headless_task_bridge.c和
headless_call_arm64.S严格编译。native bridge标记只跳过自有诊断callback，
并非关闭原始JNI调用或终态校验。

运行预检：

```sh
adb shell am instrument -w -r -e label UNIQUE_TRACE -e mode trace -e repeats 1 \
  com.nashura.benchmark/.BenchRunner
```

应用files目录保存标签.log和标签.bin；正常完成要求返回标记、
INSTRUMENTATION_RESULT: status=returned及真实finalized同时成立。
用compare_headless_snapshots.py逐tick比对私有Linux/Android归档。

无trace性能：mode=benchmark、repeats=30，每次使用新标签。
用summarize_headless_benchmark.py读取拉回的log，Android额外传
`--completion NASHURA_X86_ANDROID_PROBE_RETURN=1`。

## ARM Linux + QEMU user

沿HEADLESS_RUNTIME.md构建同一直接ART入口，服务器版本加
`-DNASHURA_QEMU_PTHREAD_ENTRY`。真实sigchain源码和头文件仍按已有固定身份获取。
私有ARM闭包内用qemu-aarch64-static显式执行原始linker64及ART入口；
使用与报告一致的bootclasspath、应用DEX、APK、引擎和fixture SHA。

服务器QEMU入口显式-Ximage:/nonexistent/task-boot.art、-Xusejit:false、
-Xnoimage-dex2oat、-Xint、-Xms16m、-Xmx256m。
保留本地property字节，QEMU root chroot中其属主需符合Bionic读取要求。
容器只bind四个基础设备及只读proc，network none，cpuset两核，6GiB限额。

性能时删除LD_PRELOAD诊断库和NASHURA_TRACE_SNAPSHOTS，
设置NASHURA_RESET_REPEATS=30、NASHURA_MEMORY_SAMPLE=1、
NASHURA_QUIET=1和NASHURA_BENCHMARK_VALIDATE=1。
先做独立归档正确性预检，再顺序测速，避免与同核Android争用。
不要将带strace/trace的预检耗时纳入结果。

原始snapshot wrapper、其他fixture、并发密度、冷启动统计和逐库最小化
仍未闭合。所有运行记录保留失败、输入身份、命令、退出码与源版本。

## 归因辅助探针

timer_probe.c比较空计时区间，cpu_probe.c比较同源整数和标量double迭代。
分别使用native x86编译器和Android30 ARM64 NDK编译，-O2，后者加
-fPIE -pie；计算探针两者均-ffp-contract=off。同一服务器同一物理核
顺序运行原生与QEMU版本，核对每轮摘要/输出，使用5轮中位值。
它们是辅助诊断而非真实libg原生x86对照，不用来预测游戏精确转译倍率。
