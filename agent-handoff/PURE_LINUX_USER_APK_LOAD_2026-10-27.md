# 用户提供 APK 核验与真实 libg 最小加载

日期：2026-10-27。任务分支 `codex/pure-linux-libg-headless-20261026`，
父提交 `2b67d0d3a9a1ae2a972d6067fff8ea3208024abc`。历史输入清单和失败报告保留。

## 结论与输入范围

用户提供的 APK 足够继续**独立原生加载实验**。实际 Ubuntu ARM64 guest 中，
既有 Bionic 探针对真实 libg 执行 `dlopen(RTLD_NOW | RTLD_LOCAL)`、`dlclose()`，
进程退出 0，取得 **LOAD_ONLY**。未调用战斗初始化、动作、tick、快照或终局入口。

此前将整包锁定 SHA 缺失作为整个原生加载实验的阻塞过于保守。本次补充了逐组件身份
证据，使用用户新提供的输入单独实验，未将其伪装成历史锁定原包，也未修改
`supported_engine.json`、app manifest、旧 gate 或冻结部署。

| 身份 | 实际核验 |
|---|---|
| 包名 / 版本 / versionCode | nullsroyale.rel.free / 15.535.13 / 150535013，aapt exit0 |
| 用户 APK SHA-256 | a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f |
| 历史 supported_engine APK SHA | 685c115ee432d5fd2d7be856a58ea0cb029c9c187aa9061f3cd250994dfbcf2a，整包不匹配 |
| 历史 E0 已安装插桩包 SHA | f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b |
| ARM64 libg SHA | 110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783，完全匹配 |
| 全部 8 个 ARM64 游戏库 | 对 app manifest 逐项 SHA 相同 |
| APK assets | 对历史 E0 包解压后逐项 SHA 相同：9,829 文件、849,482,445 bytes |
| runtime / update | 214/214、990/990 文件匹配既有 manifest |
| runtime fingerprint | e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339，匹配 |

不仅比较了资源数量或 CRC：9,829 个资产均实际读取并计算 SHA-256。
用户 APK 与 E0 插桩包的 AndroidManifest.xml、classes.dex、resources.arsc、
META-INF/MANIFEST.MF 内容不同，另有 Android res/元数据条目差异；E0 包另含 libcrprobe.so。
这些差异尚未逐项归因，因此不宣称只是签名或 ZIP 排序差异，也不将 Java 客户端视为等同。
本次 native load 没有安装该 APK、加载 DEX 或运行其 Android 资源分派。

## 实际实验

复用用户授权恢复的 Ubuntu 22.04.5 ARM64 VM。本轮重新实测 `uname -m=aarch64`。
新建独立 guest 目录，投入约 87.2 MB 未压缩传输包；运行时盘余量约 2.0 GB。
未安装组件、改变 VM 配置、启动 Android Emulator 或操作其他任务。

来源为该 APK 的游戏库及已匹配 runtime 中的静态递归候选，共暂存 185 个库。
这个数是暂存数量，绝非本次实际加载数量或必需依赖数量。库文件扁平放入新目录，
同名库须 SHA 相同且静态依赖无歧义；没有覆盖 guest 的系统库或建立 `/system`。
全部 187 个输入（185 库、linker、探针）在实验前后复核，字节未变。

下面变量表示私有隔离路径；实际 argv 和完整输出保存于私有归档。命令级
`LD_LIBRARY_PATH` 仅对子进程生效，未修改系统或全局环境。

```sh
aapt dump badging "$USER_APK"
uname -m
df -Pk "$GUEST"
LD_LIBRARY_PATH="$GUEST/libs" "$GUEST/linker64" "$GUEST/bionic-probe-v2"
LD_LIBRARY_PATH="$GUEST/libs" "$GUEST/linker64" "$GUEST/bionic-probe-v2" "$GUEST/libs/libg.so"
```

五项实际退出码均为 0。末条 stdout 为：

```text
BIONIC_SYNTHETIC_ABI_ONLY
LOAD_ONLY: no explicit lifecycle calls; constructors may have run
```

第一行是既有探针的合成 pthread/TLS 自检标记；第二行只在传入真实库且 dlopen 成功时
输出。探针在其后检查 dlclose，失败则返回 6；实际进程仍为 0。
探针源码为 `tools/pure_linux/bionic_probe.c`；其二进制 SHA
`02dbc40e7ff04eafd633d24b1c8bd69fe88959d9105c5cb8cfe4e78c7a8db9cc`，
与上一轮成功 ABI 探针完全相同。

真实加载命令耗时约 0.061 秒，只是单次进程测量，**不构成完整局吞吐基准**。
stderr 保留两条 linker 警告：libc++_shared.so 的处理器专用 DT entry
`0x70000001`（值 0）被忽略；没有将警告删除或归类为初始化通过。
未观察到 loader 报缺库、未解析强符号或本次进程崩溃；未独立采集全部实际绑定库/符号。

没有调用 JNI_OnLoad，没有创建假 JavaVM、JNIEnv、配置值或 ready 状态，没有新增 shim。
标准 dlopen 可能执行 ELF 构造器，未逐个插桩统计，不将 753 个静态 init_array 槽计为覆盖。

## 尚未验证与下一步

- 历史锁定 APK 的整包身份缺口仍存在；本次新输入不使历史 strict gate 自动转绿。
- 本次加载通过，尚无独立内容 root/context、catalog、manager 或 GameState 初始化证据。
- JNI_OnLoad 是否必要、配置/资源首次访问、动态 resolver、客户端全局状态仍为 UNRESOLVED。
- 未调用 GameStateStep 或任何战斗接口，未取得 NATIVE_MATCH_LIFECYCLE_PASS、
  PURE_LINUX_NATIVE_ENGINE=PASS、no-JavaVM 冷启动、Android-free 或 10× 结论。
- 下一步先记录实际加载映射和初始化对象来源，再在独立实例尝试内容及 manager 的最小构造；
  只补有证据的依赖，不用统一默认返回值或伪 ready。

原始实验保存在私有 `pure-linux-user-apk-20261027-xE1hhMr8` 归档；
公开只保留[脱敏证据](pure-linux-user-apk-load-evidence-20261027.json)。未发布 APK、
库文件、提取资源、原始审计或凭据。
