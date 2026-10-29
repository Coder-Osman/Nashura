# E2.1-N：android_servers 加载与系统服务依赖

日期：2026-10-29。接续原始 init property 请求边界，在同一 ARM64 Ubuntu Linux、
原始 Android 30 ART/框架及真实 servicemanager 下，继续运行系统服务入口。

## 结果

`System.loadLibrary("android_servers")` 已真实返回，
`ActivityThread.systemMain()` 返回的系统 Context 非空。限定加载状态为
`ANDROID_SERVERS_LOAD_PASS_LIMITED`；PowerManager、DisplayManager、package 服务、
GameApp Context、严格 catalog 和完整引擎链路仍未验收。

原始 PowerManagerService 的构造已经实际进入 BatterySaverController，随后在
`PowerManager.invalidatePowerSaveModeCaches` → `PropertyInvalidatedCache` →
`SystemProperties.native_set` 因缺少可写 property service 退出。服务没有启动成功。
这与 SystemServer 路径共同指向真实 property service，而非继续补入空服务或成功标志。

## 运行定位与修复

1. GDB 停在 PC `0`，LR 映射到 `libandroid_runtime.so` 文件偏移 `0xa0100`；原始
   ELF 反汇编表明前一条调用为 `JNI_CreateJavaVM`。同一配置下独立 strace 记录
   `JniInvocation: Failed to dlopen libart.so ... not found`。崩溃发生在 JavaVM
   创建阶段，细化了此前“linker 闭包不完整”的历史边界。
2. 为 system → com_android_art 的既有 namespace 链接追加精确的 `libart.so`
   导出，保留 bionic namespace 分配和原始库字节。`prepare_art_config.py` 已生成
   该配置，`art_namespace.sh` 可选 bind 外部配置，保留旧输入。由公开生成器
   产生的最终配置也在 guest 中复测通过原生库加载。
3. 原始 Android 11 `SystemServiceManager(Context)` 构造器为包可见；探针使用
   `getDeclaredConstructor` 和反射访问该真实构造器，随后通过原始 manager 启动服务。
4. DisplayThread/UiThread 的优先级需要进程资源许可。默认 `RLIMIT_NICE=0`
   时，原始 android.display 线程因 SecurityException 进入 RuntimeInit 退出路径，
   trace 明确记录自发 `kill(..., SIGKILL)`。仅为实验子进程提供 `RLIMIT_NICE=40`
   并降回普通 guest 用户后，`setpriority(...,-3/-2)` 均真实返回 `0`；外层普通
   进程的 limit 仍为 `0`。未修改框架线程优先级实现或系统级 limits 配置。

## 独立实验

| 实验 | 真实到达位置 | 退出 / 结果 |
|---|---|---|
| config-v1 | android_servers 返回、系统 Context 返回 | 3；探针公共构造器查询失败 |
| config-v2 | 原始 DisplayManagerService 启动入口 | -9；android.display 优先级异常后自发 SIGKILL |
| priority-v3 | 原始 DisplayManagerService 构造 | 3；真实 PowerManager 缺失 |
| priority-v4 | 原始 PowerManagerService / BatterySaverController 构造 | 3；真实系统属性写入失败 |
| priority-v5 | 使用公开生成器与 launcher 复测相同 PowerManager 路径 | 3；相同属性写入失败 |

五次 runtime 输入快照前后字节相同。最终原始 ART 配置 SHA 为
`76ffeacf54f846f64e011aa832bd41e7e8ae9e808e6d57e2a163527f144b8b64`，
公开生成配置 SHA 为
`92206fda5f9ff3ff5832eff1a58af9e928f916d845247c3ff53751b316caffa8`。
同镜像 ADB pairing 输入沿用上一阶段身份。运行未启动 Android 模拟器。

## 下一处运行边界

原始 init 的 property socket 已在独立阶段真实接收请求，但没有加载 policy 的
SELinuxfs 缺少 property_service class，且 user namespace 中的 init 无权写真实
sysctl。后续原始 policy / root PID namespace 实验已准备输入身份、只读预检和
回退流程；该后续实验单独记录，本报告覆盖的服务依赖实验未改变全局配置。

为保留 tmpfs 证据，ART 闭包已归档到 guest 用户目录：150,101,817 字节，SHA
`f624489227d86928f1c1126e561035b82d98a4e905abe50b5f8f12fbb5635ca8`；
独立读取归档逐项核对了 1,053 个普通文件和 44 个符号链接。另保留首次归档因
dev/.booting 权限不足失败的输出；成功归档使用新的文件名，没有覆盖失败归档。

原始 toolbox/getprop 已从同镜像只读提取；仅执行 getprop 的 root mount namespace
预检确认 sysctl 保护挂载可建立，policy_load 仍为 `0`、enforce 仍为 `0`、
kptr_restrict 仍为 `1`。这不是 property 写入/读回成功证据。

公开摘要：`pure-linux-system-service-dependencies-evidence-20261029.json`。原始日志和
第三方库留在私有证据目录。构造器与服务依赖依据原始 DEX 及
[Android 11 SystemServiceManager](https://android.googlesource.com/platform/frameworks/base/+/android-11.0.0_r48/services/core/java/com/android/server/SystemServiceManager.java)、
[DisplayManagerService](https://android.googlesource.com/platform/frameworks/base/+/android-11.0.0_r48/services/core/java/com/android/server/display/DisplayManagerService.java)。

## 工程验证

Python 3.13.5：纯 Linux `1128 passed、1 skipped`，公开主组 `794 passed、27 skipped`，
主引擎 `211 passed`，公开附加分组均 exit 0。namespace 导出回归断言、Java 编译、
shell 语法、公开源码清单、gitleaks 和 Git whitespace 检查通过。
这些工程门不等于服务启动、property readback 或完整游戏验收。
