# 原版战斗最小依赖图：E2.1-N（2026-10-27）

基线 `82b94be5ae5ceb52f8cee0f5873ec3b8845bb573`；仅匹配 SHA `110aa2b5…59783` 的 ARM64 libg。
本图是有边界的静态调查；本轮无 guest 引擎运行。历史 Android 内观察与本图分开。

```text
Apple Silicon / Darwin 开发宿主
  └─ ARM64 Linux guest [BLOCKED_ENVIRONMENT]
      ├─ direct Linux ELF syscall / clone / TLS smoke [已构建，NOT_RUN]
      └─ Android linker64 + Bionic sysroot [文件匹配，运行 NOT_RUN]
          └─ libg + 12 个 DT_NEEDED [STATIC_FOUND]
              ├─ resolver / namespace / symbol versions [UNRESOLVED]
              ├─ JNI / JavaVM / 配置桥 [UNRESOLVED]
              ├─ APK/update Provider [REPLACEABLE_CANDIDATE]
              ├─ 内容 root / catalog / postprocess 上下文 [UNRESOLVED]
              └─ manager / controller / GameState
                  ├─ construct → setup → load [UNRESOLVED]
                  ├─ command parse/validate → queue → apply [UNRESOLVED]
                  ├─ GameStateStep → read tick/state [UNRESOLVED]
                  └─ snapshot/restore → reset → destroy [UNRESOLVED]
```

## 依赖标签与证据边界

| 标签 | 本轮用法 |
|---|---|
| STATIC_FOUND | 匹配输入中的 ELF 导入、导出、调用点或既有 probe 的前置条件 |
| RUNTIME_OBSERVED | 需实际引擎运行日志；本轮 guest 中没有此类事件 |
| PROVEN_REQUIRED | 需移除/替换对照证明必要；未把任何平台依赖标成战斗必需 |
| REPLACEABLE_CANDIDATE | 资源、已观测配置、日志/属性等候选边界，尚非完成实现 |
| UNRESOLVED | 间接分派、初始化对象、动态加载、命名空间或语义待验证 |
| MISSING_INPUT | 原始锁定 APK；版本相同的本地包未替代其 SHA 身份 |

静态递归检查 185 个 ARM64 ELF，候选文件存在不等于 loader 绑定成功。
需要 JNI 的调用可能经函数表发生；没看到某个导入不证明没有该依赖。

## 候选入口与对象闭包

| 候选 | 已有证据 | 前置对象/待验证点 | 本轮结论 |
|---|---|---|---|
| GameState constructor | probe 签名为 void(object*, int32, context*, data*, int32)；有限静态闭包 129 函数、3 个直接间接调用点 | 对象尺寸/分配、context、内容和 flags | STATIC_FOUND / UNRESOLVED |
| GameState setup | void(object*, int32)，存在间接分派 | 已构造对象、mode、全局状态；setup 不等于 create | STATIC_FOUND / UNRESOLVED |
| GameState load | void(object*, input*, context*, data*)；闭包 1673 函数 | reader/stream、catalog、对象所有权 | STATIC_FOUND / UNRESOLVED |
| GameStateTick | int32(object*)；8 bytes、113 callers、0 callees | 只读取 tick | STATIC_FOUND，非推进入口 |
| GameStateStep | void(object*)；闭包 2313 函数 | 已加载 state、manager、queue、内容与虚分派目标 | STATIC_FOUND / UNRESOLVED |
| ReplayBattleController lifecycle | 2220 bytes、82 callees、14 个直接间接调用点 | manager、content、controller/scene 链接 | STATIC_FOUND / UNRESOLVED |
| 动作 parse/append/apply | 已有 probe 使用解析、队列与上下文；append 转移所有权 | 原生校验、执行 tick、拒绝语义及清理 | STATIC_FOUND / UNRESOLVED |
| StateSnapshot | 236 bytes、12 callees、3 个直接间接调用点 | stream wrapper 和 service/controller；payload 对照，不比较裸指针对象 | STATIC_FOUND / UNRESOLVED |
| reset/destroy | destroy 有限闭包 218 函数、27 个直接间接调用点 | queue、listener、对象池、内容/global 寿命 | STATIC_FOUND / UNRESOLVED |

函数闭包计数来自绑定相同引擎 SHA 的既有静态分析，不含未解析的间接边，不是运行覆盖率。
NativeReplayRunner / ReaderRegister 的旧边界与 pcode 问题未由本轮修复，也未用于声明语义通过。

## 当前客户端绑定与可剥离性

现有 serve_jni 调用 GetJavaVM，resident export 要求非空 JavaVM；inspect_content_runtime
仅检查已有内容 root/context/ready。将它移入 Linux 并传入伪指针，不构成冷启动实现。

Activity、UI、Surface、EGL/GLES、Looper、JavaVM、客户端全局场景管理器能否绕过，
全部保持 UNRESOLVED。EGL 的出现只是客户端图形引用；反过来，step 静态图没出现 JNI
也不足以证明它独立。最小实验应先定位原生内容初始化入口及其对象工厂，再逐项替换边界。

## 下一项可证伪实验

1. 先取得独立 live ARM64 Linux guest，完成合成 ABI smoke。
2. 不带 libg 参数运行 Bionic 探针；观察 linker/namespace/version 首个失败点。
3. 原包及 runtime 身份闭合后，单进程加载匹配 libg；仅加载成功记 LOAD_ONLY。
4. 在无 UI/VM 条件下创建内容上下文和 manager；逐入口检查真实状态变化和释放。
5. 首次原生初始化通过后，再做动作/tick/snapshot/reset/终局对照；最后测吞吐。

本轮未实现任何假定值 shim，未传入伪 JavaVM、默认配置或 ready 标志。

## 2026-10-27 续验后的依赖前沿

上图保留最初检查状态。授权恢复现有 VM 后，ARM64 Linux guest 已实测，direct Linux
合成 smoke 通过；匹配 Android linker/Bionic 的 pthread/TLS 合成探针也通过。
故前两层环境/合成 ABI 阻塞解除，原版引擎层仍未运行。

```text
ARM64 Linux guest [真实 aarch64]
  ├─ Linux synthetic ABI [PURE_LINUX_SMOKE_PASS]
  └─ Android linker + libc/libdl synthetic ABI [exit0；实际绑定已核对]
      └─ 真实 libg 加载 [NOT_RUN / MISSING_INPUT：锁定 APK]
          ├─ 473 imports → 469 静态候选；4 unresolved weak
          ├─ namespace、IFUNC、753 init_array 槽 [运行 NOT_RUN]
          └─ content/context → manager → GameState [UNRESOLVED]
```

静态版本符号/relocation 审计不证明运行绑定；这里 RUNTIME_OBSERVED 只涉及合成 ABI，
没有增加原版战斗热路径覆盖。详见[续验报告](PURE_LINUX_GUEST_ABI_2026-10-27.md)。

## 用户 APK 补充后的最新前沿

新输入的全部8个ARM64库、9,829个assets逐项SHA匹配既有组件；整包身份不同，
单独登记而不修改旧锁定清单。真实ARM64 Linux中已成功dlopen/dlclose原版libg，
进程exit0，取得LOAD_ONLY。没有调用JNI_OnLoad或战斗入口，没有新shim。
上文“原版加载NOT_RUN”属于此前状态，现已由该独立实验推进。

```text
ARM64 Linux + Bionic [合成ABI已通过]
  └─ 真实匹配libg dlopen/dlclose [LOAD_ONLY / exit0]
      ├─ 实际加载映射/间接resolver覆盖 [未独立采集]
      ├─ content/context/catalog → manager → GameState [UNRESOLVED / NOT_RUN]
      └─ 动作/tick/snapshot/reset/终局 [NOT_RUN]
```

历史整包锁定门未通过不再阻塞这一新输入的独立组件加载实验；它仍不能用于声称历史
Java客户端身份一致。见[新输入实验](PURE_LINUX_USER_APK_LOAD_2026-10-27.md)。

## 2026-10-29：真实 ART 与游戏 VM 绑定前沿

ARM64 Linux guest 已加载实际 Binder 模块和 binderfs，协议 8、映射及 ioctl 通过。
同一 SDK 镜像提取的 ART 执行 Java 与原生 pthread JNI 生命周期（mask 127）。精确
引擎经 Java `System.load` 后，游戏 VM 槽与 ART `JavaVM*` 相同，原生环境 getter
返回当前 `JNIEnv*`（mask 7）。15 个游戏 Java 类查找失败仍被记录；此阶段没有
重跑 holder setup、初始化 catalog 或战斗对象图。

```text
ARM64 Linux + Binder + Bionic/ART [真实 Java/JNI PASS_LIMITED]
  └─ 精确 libg System.load → VM 槽/环境 getter [PASS_LIMITED]
      ├─ APK dex / Android framework / JNI 类注册 [UNRESOLVED]
      ├─ holder setup → catalog → ResourceManager [待新运行时重验]
      └─ GameState → Step/动作 → 快照/完整对局/性能 [NOT_RUN]
```

此前空 VM fault 的前置状态已改变；真实 holder setup 已在同一 ART 闭包中完成限定
生命周期，Android/Linux 跨端身份审计 exit 0。当前依赖图为：

```text
真实 ART JavaVM / libg VM 绑定 [PASS_LIMITED]
  └─ holder setup 对象与清理 [NATIVE_VERIFIED_LIMITED]
      ├─ provider 配置语义 / GameApp Context [NOT_INITIALIZED]
      ├─ catalog → ResourceManager [CATALOG_READY_PASS=false / NOT_RUN]
      └─ GameState → Step/动作 → 快照/完整对局/性能 [NOT_RUN]
```

见[ART 报告](PURE_LINUX_ART_JAVAVM_2026-10-29.md)和[holder 报告](PURE_LINUX_HOLDER_SETUP_REAL_ART_2026-10-29.md)。
