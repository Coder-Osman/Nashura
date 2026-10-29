# E2.1-N：真实 ART 下的 holder setup 生命周期

日期：2026-10-29。引擎身份为 `libg.so` SHA-256
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
本阶段在 VMware Ubuntu ARM64 guest 的真实 binderfs/ART 闭包中运行实际 APK 的
`classes.dex`（SHA-256 `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6`）。

## 结果

阶段状态为 `NATIVE_VERIFIED_LIMITED`。精确 holder setup 入口返回 `1`；holder
slot 指向分配块的 `allocation + 0x18`，前缀的 allocator 指针和 allocation 自指针
均匹配，payload `0x60` 字节可读。holder destructor 清空 slot，分配器释放、旧
filesystem manager 替换、新 manager 保留、provider release 和 `dlclose` 均返回；
最终复跑进程 exit `0`，JNI pending exception 为 `0`。

setup 会清空 data/update provider 的路径字符串，随后 manager 仍保留为新的全局
singleton。这是 setup 的实际副作用观察，不等价于完整 Java 配置或资源语义。
`ART_GAME_CONTEXT_PRESENT=false`；`ApplicationUtilBase.getAppVersion()` 因真实
`GameApp` Context 为空抛出 `NullPointerException`，因此本阶段不计为完整初始化。

Android v4 结果与 Linux final-v1 输入完成跨端身份审计，审计进程 exit `0`，状态为
`NATIVE_VERIFIED_LIMITED`。审计确认 Android setup enter/return/init 事件关联、Linux
setup 输出顺序、输入/资源/辅助文件/probe 前后身份一致；Android update 清单稳定为
908 项。该门仍只覆盖 holder setup 生命周期，未提升 provider 配置语义或 catalog 状态。

| 门 | 状态 |
|---|---|
| ART JavaVM 与 libg VM 绑定 | `PASS_LIMITED`，mask `7` |
| holder setup native return/对象生命周期 | `PASS_LIMITED` |
| payload 前缀布局 | `PASS_LIMITED`，offset `24` |
| setup 后 provider 配置语义 | `NOT_VERIFIED`，data/update 路径长度为 `0` |
| Java GameApp Context | `NOT_INITIALIZED` |
| 严格 catalog | `false` |
| ResourceManager / GameState / Step / 动作 / 快照 / 完整对局 / 性能 | `NOT_RUN` |

## 失败链与边界

v1 将 payload 当作分配基址，allocator 检查失败并返回 `101`；v2 修正偏移后仍
复用了 setup 前的 manager 指针，清理时触发 `SIGSEGV`；v3 证明 setup 会替换
manager，v4 改为重新取得 singleton 后完整返回。以上输出和 strace 的原始字节
留在本机私有归档，未覆盖历史证据。

v1–v4 使用历史 E0 插桩 APK 的 DEX（`ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608`），
只计为限定实验。v5 和最终 app_process 复跑已改用原始 DEX `c781c71...`；该 DEX 与
用户 APK 唯一的 `classes.dex` 逐字节相同。用户 APK SHA 为
`a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`。

跨端审计曾因审计器读取错误的 JSON 层级而停止；修正为读取运行器 result 顶层的
`update_before/update_after` 后，以 Android v4 原始脚本身份复跑通过。原始失败结果
和修正后的审计结果均保留在本机私有归档。

直接构造 `GameApp` 成功；调用原始 `onCreate()` 在未附着 `ActivityInfo` 时失败。
此前 `ActivityThread.systemMain()` 在裁剪的 boot classpath 中因
`SystemServiceRegistry` 依赖缺失而失败；补入同一 SDK 的 tethering/wifi framework
jar 后，仍需真实 ServiceManager 才能提供系统服务。启用 guest 内核 SELinux 接口、
重建 binderfs 并启动同镜像原始 `servicemanager` 后，系统 Context 已真实返回：
类型为 `android.app.ContextImpl`、包名 `android`、资源对象存在，Binder
`android.os.IServiceManager` descriptor 与 ping 均通过。display/package 服务尚未
启动，`GameApp.getInstance()` 仍为空；没有创建假的 Context、PackageManager 或 ready 状态。

系统 Context 证据见[摘要](pure-linux-system-context-evidence-20261029.json)，其状态为
`SYSTEM_CONTEXT_PASS_LIMITED`。该实验使用 permissive SELinux、`policy_load=0`，不代表
完整 Android policy 语义或应用服务环境。

## 复现与身份

公开源码入口：`tools/pure_linux/ArtNativeInitProbe.java`、
`tools/pure_linux/art_native_init_driver.c`、`tools/pure_linux/provider_probe.c`、
`tools/pure_linux/provider_job.py` 和 `tools/pure_linux/audit_native_holder_setup.py`。

用 NDK 27.3 的 ARM64 API 30 clang、JDK 与 D8 构建独立 fixture（变量使用本机实际
工具路径及新建输出目录）：

```sh
"$CC" -Wall -Wextra -Werror -fPIC -Dmain=nashura_native_init_main \
  -c tools/pure_linux/native_init_probe.c -o "$BUILD/native_init.o"
"$CC" -Wall -Wextra -Werror -fPIC -shared \
  tools/pure_linux/art_native_init_driver.c tools/pure_linux/provider_probe.c \
  "$BUILD/native_init.o" -ldl -Wl,--no-undefined -o "$BUILD/libart_native_init_driver.so"
"$JAVAC" -proc:none -d "$BUILD" tools/pure_linux/ArtEngineLoadProbe.java \
  tools/pure_linux/ArtNativeInitProbe.java tools/pure_linux/ArtFrameworkContextProbe.java
"$D8" --min-api 30 --output "$BUILD" \
  "$BUILD/ArtEngineLoadProbe.class" "$BUILD/ArtNativeInitProbe.class" \
  "$BUILD/ArtFrameworkContextProbe.class"
```

运行前调用 `provider_job.entry_guards(Path(engine))`，以精确 ELF SHA 和各入口字节
校验生成 `NASHURA_<NAME>_GUARD`。真实启动设置 `NASHURA_HOLDER_SETUP_ONLY=1`、
`NASHURA_PROVIDER_MODE=both` 及独立 data/update 资源路径。CLASSPATH 由 fixture DEX
与 SHA 为 `c781c71...` 的原始游戏 DEX 组成。通过既有 namespace 脚本启动 ART APEX
目录内未修改的 app_process，沿用 ART/core、conscrypt、framework 和 statsd
bootclasspath，并设 `-Djava.library.path=/data/local/tmp/libg-input`。
`ArtNativeInitProbe` 的六个参数依次是 engine、VM observer、driver、独立输出目录、
content guard、filesystem guard；输出目录每次新建。基础 runtime/namespace/observer
构建参见[ART 复现步骤](PURE_LINUX_ART_REPRODUCING_2026-10-29.md)。

最终复跑的私有输出身份如下：

| 文件 | SHA-256 |
|---|---|
| holder stdout | `a91a5ae648130590642ecbe273d1055e4f23ac8903213b3de02aae52d22008e0` |
| holder stderr | `e7b04cadd9db60e07a79443686f78ce13055a3feb43afb775ff93fed9ba766ef` |
| holder exit | `9a271f2a916b0b6ee6cecb2426f0b3206ef074578be55d9bc94f6f3fe3ab86aa` |
| launch metadata | `e2986dbfc8d7a9b8b15f3cff842f7058cd10557d4b5539f57f3a9ef3f275cd67` |
| Activity Context stderr | `5b3a058eeac96b36c80efea1ed099b0689e6f246d58c7648d96510ea233e74b9` |
| system Context stderr | `ba9b670c2e1c53d917593efba1ab4acdce5716d8985332650381afb5fdc0cb56` |

跨端 holder 审计身份：Android result `d7b8c2549548de2d23d0ec3cbe3149cd484071d623d7c304655dde045f749f01`；
匹配脚本 `732e6b244450689bc3c94d48ce4d3a66cc960ea5cdde8b8d954247f0811b0af2`（Git
提交 `301a2ea`）；Linux result `8e88d7a942c5c472f4f8bc8baa13eb472424438c7c1a482a7704df5139ea599a`；
审计输出 `84840bc9717d669e770f9b9261bdf3ae4d0cf8683f5f20c85cd4c1d39bc6d3bc`。

复现审计的核心命令：

```sh
/opt/anaconda3/bin/python3 tools/pure_linux/audit_native_holder_setup.py \
  --android "$ANDROID_RESULT" --android-sha256 "$ANDROID_SHA256" \
  --script "$ANDROID_TRACE_SCRIPT" --linux-dir "$LINUX_AUDIT_INPUT" \
  --output "$AUDIT_OUTPUT"
```

其中 `ANDROID_TRACE_SCRIPT` 使用提交 `301a2ea` 中 SHA 为 `732e6b...` 的历史脚本，
以保持 Android result 的采集身份；当前脚本继续用于后续新运行。

工程回归：纯 Linux `1119 passed, 1 skipped`（含 inventory 层级修正回归），主引擎 `211 passed`，公开主组
`794 passed, 27 skipped`，公开附加分组均 exit `0`。这些是工程门，不替代完整
Android 服务、catalog、战斗生命周期或性能验收。
