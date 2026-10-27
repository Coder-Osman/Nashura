# 纯 Linux 原版 host：证据约束下的后续方案

续验日期 2026-10-27；实现与证据见同目录审计、ABI 报告及 dependency-report.json。
这份方案不把后续步骤写成已完成，不授权训练或 VM 配置变更。

## 已完成与首个门

已实现本地 ELF/输入审计、ABI 感知候选发现、direct ARM64 syscall smoke 和 Bionic 探针。
专项 15 项通过，两种 ELF 均交叉构建成功。首个动态门为 BLOCKED_ENVIRONMENT：
没有独立可用的 live ARM64 Linux guest；已有挂起 VM 保持原状。
原始 APK SHA 缺口在真实引擎加载前还需闭合。

## 初始化对象图与 shim 原则

原版 worker 的候选顺序：

```text
匹配 linker/Bionic → libg 装载与构造器 → 内容 root/catalog/context
  → battle manager/controller → GameState construct/setup/load
  → 原生命令校验/队列 → GameStateStep → 序列化观察
  → snapshot/restore → reset → destroy
```

当前内容 root/context 只有检查器，没有独立创建器。JNI/config、资源、客户端全局对象、
间接分派及 manager 所有权是对象图缺口。GameStateTick 仅用于读取计数，不能拿它推进。

只对实际证明必要且契约可明确的接口实现 shim：

| 边界 | 前置证据 | 接受条件 |
|---|---|---|
| 资源 Provider | 原版字节、namespace/fallback、read/seek/length/descriptor/missing | 完整生命周期与逐状态对照；实际平台资源请求为零 |
| 配置 | 真实键、类型、读写及阶段 | 未知键/写入显式失败，不伪造 ready |
| 日志/属性 | 实际 caller、值和副作用 | 保留语义，不统一返回默认值 |
| 时钟/线程/TLS | guest ABI 探针和原版调用日志 | 时钟来源及线程模型明确，不用墙钟 sleep 冒充 tick |
| JNI/全局对象 | 创建路径、生命周期和真实调用 | 无 JavaVM 冷启动须单独验证；不传伪 VM 或空 context |

此表为候选，不是本轮已经证明必需或实现的 shim 清单。

## 分阶段运行门

1. 环境：live guest `uname`、系统、网络和隔离根身份；不恢复或改写已有挂起 VM。
2. ABI：direct 合成 smoke 全项；随后无 libg 的 Bionic 探针。
3. Load：身份完全匹配后单进程 dlopen 真实 libg，定位首个依赖；成功只记 LOAD_ONLY。
4. 生命周期：构造内容/manager/state，原生 step 有真实状态变化，动作拒绝正确，reset/destroy 可重入。
5. 正确性：匹配输入、动作、随机流的逐 tick/决策边界 payload、snapshot/restore 和自然终局对照。
6. 性能：正确性门闭合后，在同等硬件与完整轨迹上测与 Android 基线的倍率，才考虑 10×。

每个门独立；LOAD_ONLY、合成 smoke、单测、Android 内 headless 均不替代后续原版门。

## IPC 与 host 的最小范围（尚未实现）

先每进程一局，Unix domain socket，消息包含协议版本、request id、engine/resource SHA、
tick 和状态摘要。候选命令 PING、CREATE_MATCH、APPLY_ACTIONS、STEP_TICKS、OBSERVE、
STATE_DIGEST、RESET、TERMINATE。STEP_TICKS 必须调用真实 GameStateStep；
动作分别返回 accepted/rejected 与证据；崩溃或状态差异 fail closed，不切换自有模拟器。

不在加载/对象图缺口解决前实现大套 host 或 framework。后续性能记录完整局/秒、tick/秒、
startup/reset、纯推进、IPC、CPU/RSS、完整终局率和拒绝率；未获正确性证据前不执行多 worker benchmark。
