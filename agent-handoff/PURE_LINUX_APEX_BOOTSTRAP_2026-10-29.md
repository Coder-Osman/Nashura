# E2.1-O：原始 APEX 激活与持续属性服务

日期：2026-10-29，基线 `a7fad17`。Ubuntu ARM64 guest 中的原始 Android 30 init
已通过原始 apexd 校验并挂载五个 bootstrap APEX，原始 BoringSSL 64 位自检返回
0，越过上一阶段解释器缺失的退出点。状态为 `ORIGINAL_APEX_BOOTSTRAP_PASS_LIMITED`。
init 随后等待真实 coldboot；v14 进一步启动了原始 hwservicemanager 与 suspend HAL，
并观察到 `ISystemSuspend/default` 注册；HAL 因隔离 guest 缺少 `/sys/power/wakeup_count`、
`/sys/power/suspend_stats` 和 `/sys/class/wakeup` 仅达到有限启动。系统服务、GameApp Context
和完整游戏链路继续未验收。

## 真实运行结果

从同一 `system.ext4` 提取 22 个原始 APEX，总计 262,702,069 字节，逐文件固定
SHA-256，传入只读 `/system/apex`。原始 `ro.apex.updatable=true` 保留。
三次独立输出均记录五次 AVB footer 校验、公钥匹配和实际 ext4 挂载：

| 原始包 | 实际挂载模块 |
| --- | --- |
| `com.android.i18n.apex` | `com.android.i18n@1` |
| `com.google.android.tzdata2.apex` | `com.android.tzdata@300900700` |
| `com.android.art.debug.apex` | `com.android.art@1` |
| `com.android.runtime.apex` | `com.android.runtime@1` |
| `com.google.android.os.statsd.apex` | `com.android.os.statsd@300900700` |

apexd 的汇总行虽显示 `Activated 22 packages`，bootstrap 筛选下实际挂载为上述
五个；本报告按逐包挂载事件计数。原始 linkerconfig 返回，自检
`boringssl_self_test64` 退出 0，init 随后停在 `wait_for_coldboot_done`，由 20 秒
观察上限及终止收尾停止。该结果不代表完整 Android 启动。

最终源码 v12 与 v11 均在同次 init 中独立写入并读回
`sys.system_server.start_count=1、2、3`。首次读回后运行真实系统服务探针，随后
两个值仍可读回。原始 `android_servers` 加载、系统 Context 返回；PowerManager
中的 `cache_key.is_power_save_mode` 经 property v2 收到四字节成功 ACK，该缓存键
尚无独立 getprop 读回。服务随后反复等待 `hwservicemanager.ready`，9 秒观察超时；
`ART_POWER_SERVICE_STARTED`、DisplayManager 和 display Binder 均未观察到成功。

## 设备隔离、首次失败与恢复

`art_namespace.sh` 可选将固定身份的包目录只读 bind 到 `/system/apex`，仅提供
本轮空闲/新建 loop 的 block 节点与对应 sysfs 子目录。sysfs 保持只读，仅这些
loop 的 `queue/read_ahead_kb` 供原始 apexd 配置。原有已占用 loop、guest LVM 和
device-mapper 控制节点均未暴露给 apexd。内置包仍执行原始签名验证；按 AOSP
原始分支，这类包直接挂 loop，本轮没有创建或修改 dm 映射。

`apex_loop_devices.py` 记录前后状态，租用原有空闲 loop12，创建 loop13–56；
收尾仅解除经身份核实的本轮 APEX 绑定，移除本轮新设备，并恢复原有空闲设备
的 read-ahead。原有 loop0–11 的 snap 绑定保持。

首次 v10 在功能上完成五包挂载、自检和三次属性读回，但清理路径检查拒绝了
五个 loop：pivot_root/卸载 oldroot 后，sysfs 将 backing file 显示成
`/包名.apex`。该次结果保留为 `INIT_ROOT_NOT_VERIFIED`。修复使用
`LOOP_GET_STATUS64` 返回的 backing device/inode 与原始包文件的实际 stat 身份
匹配，确认五项后完成专门恢复；原失败结果保留。v11、最终源码 v12、v13 和 v14 均自动清理
成功，新建设备全部移除，原有设备状态一致。测试也验证未知占用者保留和部分
分配失败的收尾。

三次均恢复 `kptr_restrict=1`、`mmap_rnd_bits=33`、`mmap_rnd_compat_bits=16`。
复用已有 permissive policy，加载次数仍为 1；没有再次加载 policy 或自动重启。
第三方包、ELF、DEX、policy 和原始日志继续仅保存在私有目录。

## 复验与工程检查

沿用 [property 阶段复验入口](PURE_LINUX_INIT_PROPERTY_READBACK_2026-10-29.md)，
在私有 settings 增加 canonical `apex_packages` 目录，并把每个 `.apex` 的绝对
路径及 SHA 纳入 `identities`。默认命令只校验和输出计划；guest root 中加
`--apply` 后才分配 loop 和执行 init。已有 loop ID 须从 0 连续，避免原始 apexd
额外分配未追踪的缺口设备。输出必须为新目录；首次失败和恢复使用独立目录。

服务复验继续使用 `services=true`、同镜像 services DEX 与原始程序，以及
binderfs 既有 UID/GID；空输出、存活和 exit 0 均不设置服务 ready。
v12 settings 记录的四个运行源码 SHA 均匹配其执行输入；v14 的 namespace launcher、
root-init probe 与 loop manager 也匹配 v14 settings 中的输入身份。v14 运行输入前后
SHA 一致。
Java 探针返回 3，`PowerManagerService` 已启动；`DisplayManagerService` 在构造时因缺少
已发布的 PowerManager 对象抛出 `NullPointerException`。HAL strace 记录接口注册成功，
随后报告缺少上述 `/sys/power/*` 节点。v14 输入前后身份一致，policy 仍为一次加载、
enforce=0，三项 sysctl 和 loop 设备均恢复。

纯 Linux 工程测试 `1138 passed、1 skipped`；公开主组 `794 passed、27 skipped`，
其余公开分组通过；主引擎 `211 passed`。公开检查在隔离快照执行，保留仓库已有
历史 XML。源码清单、gitleaks 和 Git whitespace 检查结果随本次发布核对。

技术依据为 [AOSP Android 11 apexd.cpp](https://android.googlesource.com/platform/system/apex/+/refs/heads/android11-release/apexd/apexd.cpp)、
[apexd_loop.cpp](https://android.googlesource.com/platform/system/apex/+/refs/heads/android11-release/apexd/apexd_loop.cpp)、
[apexd_main.cpp](https://android.googlesource.com/platform/system/apex/+/refs/heads/android11-release/apexd/apexd_main.cpp)
与实际 guest ioctl、挂载和服务 trace。本轮工具集没有 Context7，直接读取 AOSP。

下一步处理 guest 电源 sysfs 闭包与服务发布顺序，复测 PowerManager/DisplayManager；
本轮已确认 suspend HAL 的真实注册边界，但不把它等同于系统服务 ready。严格 catalog、ResourceManager、GameState、Step、动作、快照、完整局、
Android 对照和性能门均保持未通过。详见[机器证据](pure-linux-apex-bootstrap-evidence-20261029.json)。
