# E2.1-N：真实 ART/JavaVM 与 libg 绑定阶段

日期：2026-10-29。本阶段在 VMware Fusion Ubuntu ARM64 guest 中使用 Android 30
Google APIs arm64-v8a 镜像提取的 ART/APEX 闭包，配合 guest 的真实 binderfs 设备，
验证 JavaVM、JNI 线程生命周期以及精确身份 `libg.so` 的 VM 绑定。闭包和原始镜像
只在临时目录使用，不发布第三方二进制、APK、权重或机器凭据。

## 结果

| 门 | 结果 |
|---|---|
| guest 架构/内核 | `aarch64` / `6.8.0-138-generic` |
| Binder 设备 | `/dev/binderfs/binder`，协议 8，1 MiB 映射，空 `BINDER_WRITE_READ` 返回 |
| ART 闭包 | linker、libart、boot image、system/APEX 依赖解析通过 |
| property bootstrap | 47 项 SDK `build.prop` 属性添加成功；`ro.product.cpu.abilist64=arm64-v8a` |
| SELinux 等价 | 未宣称；`fsetxattr` 返回 `EPERM`，`PROPERTY_AREA_INIT=-1` 已保留 |
| JavaVM/Java 代码 | `ART_RUNTIME_PASS_LIMITED`，sum `49995000`，Dalvik `2.1.0`，exit 0 |
| JNI 生命周期 | `ART_JNI_LIFECYCLE=127`：真实 VM、主线程 `GetEnv`、pthread attach/callback/detach 全部通过 |
| `libg.so` System.load | 精确 SHA `110aa2b5…59783` 装载并返回；15 个 Java 类查找失败日志保留 |
| 游戏 VM 绑定 | VM 槽与 ART `JavaVM*` 匹配，游戏环境 getter 返回当前 `JNIEnv*`，mask `7`，exit 0 |
| holder setup / catalog | 本阶段未执行；仍需补齐实际 APK dex/framework 类并运行真实入口 |

`app_process64` 原始身份为
`6a49f8d4844ba11da113d21ec304483496ca635996aace10e588c8ff449b5b1b`，
`linker64` 为 `e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7`，
`libart.so` 为 `35a64b7c823728e10e5dee31689977417d5556a5358e4fecc568e1b133baed66`。
精确引擎 SHA 与既有阶段一致，没有替换输入。

## 失败链与边界

1. 无 property area 时，`app_process64` 因缺失 `ro.product.cpu.abilist64` 以 134 退出。
2. 属性可读后，`--version` 只因参数不适用于该入口退出 1；strace 显示真实 Binder
   `BINDER_VERSION` 和 `BINDER_SET_MAX_THREADS` 均返回 0。
3. 首次 `Hello.dex` 因 boot classpath 顺序、APEX namespace 命名及 bionic TLS 重复
   先后失败；逐项修正后，boot image 映射、`Runtime::Start` 和 Java 输出通过。
4. 引擎观察在缺少 `conscrypt.jar` 时触发 Java provider 初始化失败；加入同一 SDK
   APEX 的 jar 后通过。`System.load` 仍输出 15 个 `ClassNotFoundException`，因此
   只计为 `LOAD_AND_VM_BINDING_PASS_LIMITED`，不计为完整 Java 注册或 engine ready。

失败证据没有覆盖或重写历史报告：

| 证据 | SHA-256 |
|---|---|
| 无属性 `version-v1.strace` | `cb2dc27f9e67d791c2b199f23e3daada7f0e71edcf7bba56be7d7c46cccc066c` |
| 属性后 Binder/参数边界 `version-v2.strace` | `7871a1f0bb875f18caf8a6bdcaff6fb469b39af8f1712c6b881552f1615d483c` |
| boot classpath/namespace 失败 `hello-v1.strace` | `34ab1db6ef215e508700fb61b20e55118efce93948e6482065e8f5b029e845f8` |
| bionic TLS 失败 `hello-v2.strace` | `fac45a4e7c95ac6cd3cbc2ccbf684eb19c272677817c54783707a3fe7665215a` |
| statsd 依赖失败 `hello-v3.strace` | `eb4ae15d1518761a92b77e1d2f027f2b2821833376adbffc3dae7a6552303a25` |
| Java 成功 `hello-v4.strace` | `89e2e365296c43a2407d5a97c447238e5101dab5c50fcd5894677c06727c5a12` |
| JNI 生命周期成功 `public-jni.strace` | `2cf64c1403130f72e41e0b6481d1e928d71af52802a9b6840c6cac5ab89e6a62` |
| 引擎 VM 绑定成功 `public-engine.strace` | `0fc47d4a45097f57be77df15d39561085722dc27cc0250eb38ee8da483234858` |

## 可复跑入口

公开脚本为 `tools/pure_linux/art_namespace.sh`、`prepare_art_config.py`、
`art_property_info.cpp`、`art_property_bootstrap.c`、`art_jni_probe.c`、
`art_engine_jni_probe.c`、`ArtRuntimeProbe.java` 和 `ArtEngineLoadProbe.java`。
配置生成器要求 stock ART `ld.config.txt` 的精确 SHA，并拒绝意外重复/缺失行；定向
测试为 3 passed。运行时脚本不设置全局 `LD_LIBRARY_PATH`，避免重复加载 bionic TLS，
并把 binder 设备限定为真实 binderfs 字符设备。

闭包准备及编译命令见[复现步骤](PURE_LINUX_ART_REPRODUCING_2026-10-29.md)。

本阶段没有写入 `ready`、伪造 `JavaVM`、伪造 catalog、调用战斗入口或宣称完整引擎。
ResourceManager、GameState、Step、动作、快照、完整对局、Android 基线对照及性能门
保持 `NOT_RUN/UNRESOLVED`。

## 工程验证

纯 Linux 专项 `1108 passed, 1 skipped`，主引擎 `211 passed`；公开主组
`794 passed, 27 skipped`，其余公开分组均 exit 0，合计 `1104 passed, 42 skipped`。
这些为工程回归，独立于真实 ART/JNI 与游戏 VM 绑定观察。`gitleaks` 未发现凭据。
