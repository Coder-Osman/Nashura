# E2.1-N：最小 Context 下的原始原生生命周期

日期：2026-10-30。基线 `25a1f90`，仅在
`codex/pure-linux-libg-headless-20261026` 分支工作。
状态：`HEADLESS_NATIVE_LIFECYCLE_OBSERVED_LIMITED`。
严格 `CATALOG_READY_PASS=false`，完整目标继续进行。

## 路线纠偏与实际结果

此前 system_server、Power/Thermal、init/APEX、Display、SurfaceFlinger、GPUService、
allocator/composer 的实验主要证明这些服务自身的启动依赖，没有证明它们是战斗所需。
历史报告保留，但这些服务不再作为 headless 战斗的默认前置条件。新增依赖应有真实
引擎调用证据，并优先在调用边界提供最小语义；外围服务启动失败本身不构成建设理由。

本轮在真实 ARM64 Ubuntu Linux、Bionic/ART 与既有 Binder 接口上运行原始 APK DEX
和精确 libg，无 Android 模拟器。新探针未调用 `Activity.onCreate`、
`ActivityThread.systemMain`、`SystemServer.main` 或图形服务启动器；使用现有
`art_namespace.sh` 的普通 user namespace 路径，没有启用 root-init 或 graphics 选项。
这证明当前有限路径可不启动这些外围服务，不证明整个 ART/框架闭包已达到绝对最小。
guest 仍保留此前已加载的 permissive SELinux policy，本轮未加载或撤回 policy。

v17 的原始 `nOnNativeLibrariesLoaded`、`nOnApplicationCreate`、`createGameMain`、
`nOnCreate`、`nOnStart`、`nOnResume`、`nOnPause`、`nOnStop`、`nOnDestroy` 均返回，
进程自然退出 0。未用 `System.exit(0)` 或 ready 字段写入收尾。创建后 1 秒和约
10 秒时，Android owner、holder、引擎 game、filesystem 槽实际非空；holder 虚表为
`0x18c8be0`，game 虚表为 `0x17ea568`，后者匹配历史 Android owner 构造观察。
这里的 game 是客户端引擎对象，不等于战斗 GameState。

两次 Mainloop 采样均经过 `0x139e5bc → 0x130fb68 → 0x130f76c`，位于引擎事件循环
等待路径。采样是 PC/LR 与可读 frame-pointer 链的瞬时观察，部分帧未解析，
不视为完整调用覆盖。销毁后 owner/filesystem 槽为 0，Mainloop 线程消失。
内容 root `0x19ad878` 仍为 0，context/ready 为不可取得；ResourceManager、GameState、
reset/step/action/snapshot、完整对局与性能仍未验证。

## 有调用依据的最小边界

| 边界 | 原始调用证据 | 本轮实现与适用范围 |
|---|---|---|
| AssetManager | `createGameMain` 参数及 Mainloop 的 getAssets 路径 | 原始 APK 通过真实 AssetManager 加载；不是资源内容替身 |
| APK 元数据 | 原始 getAppVersion 和 PackageManager 查询 | 从 APK 二进制 Manifest 读取当前包名/版本；仅当前包、flags=0 |
| HPE 特性 | createGameMain 查询 `com.google.android.play.feature.HPE_EXPERIENCE` | 当前 Linux fixture 无 HPE，返回 false；其他特性查询显式失败 |
| Resources | v10 的 Mainloop：`0x139e2dc → 0x13f4e04 → 0x140d6c4 → 0x140da08 → 0x140db4c`，随后 ContextThemeWrapper.getAssets/getResources 空对象错误 | 同一真实 AssetManager 的 Resources；固定 720×1280、dpi320、density2、Locale.US；不创建 Display |
| Activity base Context | 原始 Activity 无 base 时查询 APK 信息失败 | 只绑定 ContextWrapper.mBase；绕开 attachBaseContext 的 autofill/UI 扩展；不模拟完整 Activity |
| launch Intent | v16：`nOnResume → 0x141b7e8 → 0x14006b4` 的 CallIntMethodV 空对象；静态字符串为 getFlags | 本地空 Intent、flags=0、无外部启动数据；不承诺 deep link、新文档或任务栈语义 |
| 日志与观测 | 原生日志和 JNI 注册 | 进程内 logger；同线程 RegisterNatives 临时转发，保留原注册调用并在加载后恢复 JNI 表；不启动 logd |

`HeadlessApkContext` 的未知系统服务请求抛出异常。生成的 PackageManager 基类对未实现
接口计数并抛出异常；v17 的 rejected PM 调用数为 0。其他 ContextWrapper 方法仍可能
通过空父 Context 失败，未承诺签名、安装包枚举、完整 PackageManager、preferences、
UI、Display、音频设备、联网服务或后台生命周期等价。SDK35 用于 Java 编译，运行框架
为 Android30；本轮运行覆盖的接口之外仍有版本边界。原始 UnityAdsTitan 类查找异常
仍存在并保留，当前生命周期能够继续不代表广告路径可用。

## 失败历史与观测器修正

私有目录 `nashura-headless-create-20261030-v1` 至 `v17` 独立保存，未覆盖早期证据。

| 尝试 | 退出 | 主要边界 |
|---|---:|---|
| v1 | 139 | 旧 linker 配置缺 system→ART libart 导出 |
| v2 | 134 | 原始 Activity 空 base Context |
| v3、v4 | 3 | PackageManager 元数据解析、Activity autofill attach 路径 |
| v5、v6 | 0 | HPE 未实现异常被原生代码吞掉，返回不等于成功 |
| v7 | 137 | generated PM 计数字段与观察代码不匹配 |
| v8、v9 | 0 | 最小 APK 元数据下 create 返回；v9 记录真实 JNI 注册 |
| v10 | 134 | Mainloop 的 Resources 空对象错误 |
| v11、v12 | 124 | 主线程结束后 native 线程仍活，外层超时 |
| v13 | 0 | 原始 nOnDestroy 收尾；早期 holder/game 读数仍不可靠 |
| v14 | 0 | 明确发现 holder/game READ_FAILED，Mainloop 已在事件循环 |
| v15 | 0 | 去除读取偏移的 ARM64 顶字节标签后，holder/game 实际非空 |
| v16 | 134 | native resume 读取空 Intent |
| v17 | 0 | 最小本地 Intent 下原始原生生命周期返回并自然收尾 |

v12/v13 的旧 `read_pointer` 将 pread 失败折叠为 0，误把带标签堆指针读取失败当作
holder/game 为空。该结论撤回。v14 显式区分 READ_FAILED；v15 起只对
`/proc/self/mem` 的文件偏移去除 ARM64 顶字节标签，保持所有引擎指针原值。
父槽为空、读取失败和真实 null 分别记录，不写入引擎对象。

createGameMain 返回的 40 字符串来自 ELF 静态地址 `0x2ba599` 的无条件 NewStringUTF，
不作为错误/成功 token。日志只记录是否为空与 UTF16 长度。所有运行保持
`HEADLESS_BATTLE_READY=UNVERIFIED`。

## 构建与复验

操作者提供同版本 APK/DEX、匹配 libg/原始依赖、Android30 ART 闭包、JNI VM 观察器及
Binder 接口。第三方输入不随仓库发布；APK、DEX、libg 的固定 SHA 在 Java 探针中校验。
构建命令中的输出目录须新建，生成器拒绝覆盖已有 Java 输出。

```sh
mkdir "$OUT" "$OUT/classes"
python3 tools/pure_linux/generate_headless_pm.py --javap "$JAVAP" \
  --android-jar "$ANDROID_JAR" --output "$OUT/UnsupportedHeadlessPackageManager.java"
"$JAVAC" --release 8 -cp "$ANDROID_JAR" -d "$OUT/classes" \
  tools/pure_linux/ArtEngineLoadProbe.java tools/pure_linux/ArtHeadlessCreateProbe.java \
  tools/pure_linux/HeadlessApkContext.java "$OUT/UnsupportedHeadlessPackageManager.java"
"$D8" --min-api 30 --lib "$ANDROID_JAR" --output "$OUT" "$OUT"/classes/*.class
"$CLANG" -std=c11 -shared -fPIC -O2 -Wall -Wextra -Werror \
  tools/pure_linux/art_headless_observer.c -o "$OUT/libheadless_observer.so" -llog -ldl
```

沿既有 ART namespace/bootclasspath 运行 `ArtHeadlessCreateProbe`，参数依次为：
精确 libg 路径、JNI VM 观察器、原 APK、新建 run root、`callbacks`、`apk-context`、
headless observer、`native-create`、`native-destroy`、可选 `native-active`。
v15 不带最后一项；v17 包含最后一项。外层 timeout35秒、kill-after3秒并保存
file/process/network/ioctl strace。观察器使用一个空闲实时信号做进程内 Mainloop
采样，检查既有 handler 并保留采样 handler 到进程结束；线程未响应则明确记录超时。
信号采样存在调度扰动，不能用此运行测性能。

原始日志、完整运行参数、第三方文件及进程路径留在私有证据目录；公开 JSON 保存
各次文件 SHA、退出码、必要观测和明确未闭合门。
下一步沿真实 owner 初始化入口 `0x756be8`、其原有上下文参数和内容初始化调用链推进，
不以 Surface/EGL 的客户端初始化分支作为战斗热路径已获证的替代。

## 工程验证

Java8/D8 min-api30 构建、NDK Android30 C11 `-Wall -Wextra -Werror` 编译通过。
PackageManager 生成器已用当前 SDK35 再生成，并与实际 v17 编译输入逐字节核对。
纯 Linux 专项 `1140 passed、1 skipped`，主引擎 `211 passed`；公开检查共
`1104 passed、42 skipped`，全部分组 exit0。源码快照与 gitleaks 通过。
公开/主引擎检查沿用原脚本，仅将 JUnit 输出重定向到新的私有验证目录，保留原有
publication XML 历史字节。XML、构建输入和日志 SHA 见机器摘要。
这些工程测试不替代真实 reset/step 或完整对局验收。
