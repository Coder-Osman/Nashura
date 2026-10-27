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
