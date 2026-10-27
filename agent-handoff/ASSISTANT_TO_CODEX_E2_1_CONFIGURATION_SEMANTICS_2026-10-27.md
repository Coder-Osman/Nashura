# E2.1 操作任务书：闭合排除配置键与初始化语义

> 目标分支：`codex/headless-engine-plan-20261025`
>
> 当前基线：`db81efceb99b1b46e12d1d2b14c4f89a344daad4`
>
> 前一阶段：32 个配置键 native replacement 已通过固定完整轨迹；46 个排除键仍有约 99 次原路径 fallback；E2 strict gate 为 BLOCKED。
>
> 任务性质：直接执行、验证、提交证据。不要只写计划。

## 1. 本轮目标

本轮只推进 E2.1，不直接宣称 no-VM 或 Android-free。

目标是：

1. 对 46 个排除键逐项建立来源、类型、线程、阶段、调用者和语义账本；
2. 区分“必须替换”“可以从战斗路径移除”“暂时未知”三类；
3. 优先闭合 `PN_POPUP` 以及默认值、缺失值、异常、重复初始化、持久化、close/free 和引用生命周期；
4. 将已确认等价的排除项逐组接入 native replacement；
5. 为后续 no-VM 冷启动补齐文件系统、SCDB、配置、内容初始化之间的对象契约；
6. 重新运行 E2 strict gate，并明确记录仍然阻塞的项。

本轮不能把“fallback 减少”“固定轨迹继续通过”写成“JavaVM/JNI 已全部移除”。

## 2. 硬约束

- 只在 `codex/headless-engine-plan-20261025` 或从当前提交派生的任务分支工作。
- 不修改 `main`、`origin/main`、冻结部署、训练后端、G0/G1 运行目录。
- 不启动正式训练，不进行 10× 吞吐结论。
- 不修改原始 APK、libg、libfmod、probe、update 文件和既有身份摘要。
- 保留 E0、E1、E2 旧报告及所有失败证据；不覆盖历史 BLOCKED 结果。
- 未知键不能伪造默认值或成功返回。
- 不能把单元测试、模拟 JNI fixture 或诊断性 flag 当成真实 native 证据。
- targeted strict gate、no-VM、Android-free 必须独立验收。
- 若一个命令失败，保存退出码、stdout/stderr、环境和输出目录后继续其它独立工作；不要因为第一个问题提前结束。

## 3. 开始时检查

先运行并保存：

```sh
git status --short
git branch --show-current
git rev-parse HEAD
git log -5 --oneline --decorate
git show --stat --oneline db81efceb99b1b46e12d1d2b14c4f89a344daad4
```

然后读取：

- `AGENTS.md`
- `agent-handoff/E2_JAVAVM_CONFIG_JNI_REPLACEMENT_2026-10-27.md`
- `agent-handoff/CODEX_REPLY_2026-10-27_E2.md`
- `agent-handoff/e2_gate.json`
- `agent-handoff/e2_dynamic_dependency_counts.json`
- `agent-handoff/e2_failure_index.json`
- `tools/e2_jni_config_observer.js`
- `tools/e2_config_replace.js`
- `tools/e2_config_store.c`
- `tools/run_e2_observed.py`
- `tools/run_e2_series.py`
- `tools/verify_e2_gate.py`
- 现有 E0/E1 资源 Provider、snapshot verifier 和原生运行器

确认当前主线仍为：

```text
0012201830cfbfcbfeb3d9dccd91a855028da869
```

## 4. 阶段 A：建立 46 个排除键账本

不要直接编辑 replacement 表。先从私有原始证据和原始配置观测构建 `e2_1_exclusion_ledger.json`。

每个排除键至少记录：

```json
{
  "key": "...",
  "operations": ["read", "write"],
  "value_type": "string|array|object|unknown",
  "observed_values": [],
  "original_return_status": "decoded|empty|exception|unknown",
  "first_phase": "...",
  "last_phase": "...",
  "threads": [],
  "callers": [],
  "jni_operations": [],
  "fallback_count": 0,
  "side_effect": "none|counter|persistent|object|unknown",
  "stateful": true,
  "replacement_class": "required|removable_after_proof|unknown",
  "evidence": [],
  "reason_if_excluded": "..."
}
```

必须区分：

- 46 个唯一排除键；
- 99 次 fallback 事件；
- fallback 是配置读取、写入、初始化探针、后台线程还是间接调用；
- 发生在 cold start、load、preinit、battle tick、reset、destroy 还是 teardown；
- 同一键是否在不同阶段返回不同值。

对于每个 fallback 事件保存 caller、线程、native scope、阶段、调用栈摘要、输入摘要、返回值摘要和错误状态。未能采样的字段显式写 `unknown`，不得填猜测值。

## 5. 阶段 B：优先闭合 PN_POPUP

`PN_POPUP` 已导致 no-VM v6 在真实 fallback 处崩溃，是当前最小关键阻塞。

使用原始 Android/JavaVM 路径作为 oracle，建立下列真实矩阵：

| 场景 | 必须观察 |
|---|---|
| 首次冷启动 | 初始读取值、写回值、返回码、异常 |
| 同一进程重复初始化 | 是否递增、覆盖或保持 |
| reset 后再次初始化 | 值和序列是否重置 |
| 新进程重新打开 | 是否持久化 |
| 缺失 key | 返回值、默认值、错误码 |
| 空值 | 与缺失是否相同 |
| malformed 值 | 是否拒绝、降级或保留 |
| 并发读写 | 锁、顺序、可见性 |
| close/free 后访问 | 错误码和对象状态 |
| 异常路径 | Java exception、native error、清理顺序 |

矩阵必须至少包含 original、shadow、native replacement 三种结果。不能只把上一轮观测到的字符串写死进 native store。

如果它是计数器或持久化状态，native store 必须实现相同的：

- 初始状态；
- 递增/覆盖规则；
- reset 规则；
- 新进程行为；
- commit/apply 或等价写回时机；
- 缺失与错误语义；
- 并发和释放语义。

## 6. 阶段 C：配置语义差分矩阵

对每一个准备替换的键或键组，运行：

- normal value；
- missing；
- empty；
- malformed；
- 最短值、最长允许值、越界值；
- 只读写入；
- 未初始化读取；
- 重复初始化；
- reset/reinit；
- close/free 后访问；
- 异常/错误返回；
- 并发读写；
- 字符串、UTF-8/MUTF-8、数组和对象返回。

对照内容必须包含：

- 返回值和返回类型；
- 错误码；
- exception pending/clear 状态；
- native 对象地址之外的稳定摘要；
- 输出长度和字节 SHA256；
- 资源/配置读取顺序；
- state digest、events、legal masks、rewards、terminal reason；
- snapshot/restore/reset 结果。

输出：

```text
agent-handoff/e2_1_semantics_matrix.json
agent-handoff/e2_1_semantics_matrix.md
```

任何只在 fixture 中通过、但未在真实引擎入口验证的项都标记为 `fixture_only`。

## 7. 阶段 D：按语义分组接入 replacement

分组顺序：

1. 稳定只读键；
2. 幂等写键；
3. `PN_POPUP` 等计数/持久化键；
4. 默认值和缺失语义；
5. 异常与错误路径；
6. 对象、数组、字符串和引用生命周期；
7. 初始化顺序中涉及 SCDB、内容表和状态对象的键；
8. 后台线程或间接调用。

每完成一组，必须独立运行：

1. original；
2. shadow；
3. replacement；
4. strict-targeted。

每组都要有独立输出目录和提交 checkpoint。未完成的键继续显式 fallback，不得混入已验证 native store 的统计。

replacement 必须：

- 使用窄 host ABI；
- 使用显式 typed errors；
- 不依赖 JavaVM、JNIEnv、Android Context 或隐式全局状态；
- 保留线程安全、reset/reinit、destroy、close/free 语义；
- 保留原始输入和 Provider 身份；
- 不通过诊断性 flag 跳过初始化。

## 8. 阶段 E：初始化对象契约

根据真实调用顺序逐层核验：

```text
FileSystemBootstrap
→ ProviderBootstrap
→ APKProviderRegister
→ ReaderRegister
→ SCDBOpen
→ GameConfigurationInit
→ GamePreinit/Configure
→ ContentBootstrap
→ TableLoadBatch
→ StateMasks/IDs
→ GameState
```

每一层记录：

- 入口地址和 caller；
- 参数类型、大小、对齐和所有权；
- 返回对象布局或稳定摘要；
- 初始化、重置、销毁顺序；
- 跨线程可见性；
- 失败时的清理顺序；
- 与原始 Java/Android 对照的差异。

先完成前一层的契约，再进入下一层。不要用“最终快照一致”掩盖中间对象契约未知。

## 9. 阶段 F：重新验证三道独立验收门

### G-E2.1：配置 ABI 与语义门

必须满足：

- 46 个排除键都有明确分类；
- 所有标记为 `required` 的键已替换；
- missing/empty/malformed/boundary/normal 语义通过；
- 默认值、异常、重复初始化、持久化、close/free 和引用生命周期通过；
- replacement 运行中不再出现未解释 fallback；
- 负向篡改会被验证器拒绝。

### G-E2.2：fresh-process no-VM 冷启动门

只有 G-E2.1 通过后才做：

- 新进程；
- 不创建 JavaVM；
- 不调用 JNI；
- 对 VM/JNI 访问 fail-fast；
- 逐步通过初始化链；
- 最终到达首个确定性 battle tick；
- 保存所有失败符号、线程、阶段和调用栈。

单个 preinit 入口返回不算通过。

### G-E2.3：完整生命周期门

G-E2.2 通过后才做：

- 完整 3,601 tick；
- 28 actions；
- 3,602 snapshots；
- snapshot/restore；
- reset；
- destroy；
- 第二次 reset；
- fresh process 重跑；
- 与 original 逐字节比较。

Android-free 仍然是另一道门，不能由 E2.1 或 E2.2 自动推出。

## 10. 长时运行与失败处理

使用当前环境允许的最大执行窗口：

- 长测试自然完成，不因耗时主动终止；
- 每个失败尝试保存完整 stdout/stderr、退出码和 SHA；
- 至少尝试一种低风险替代验证路径；
- 私有附件不可用时，完成所有公开静态、fixture、ABI 和失败拒收测试；
- 每个 coherent milestone 单独提交；
- 失败证据追加，不覆盖；
- 所有原生实例、adb、AVD、监听器结束后核验回收；
- 如果阶段 A–E 已完成但严格门仍阻塞，提交 BLOCKED 报告并继续能独立执行的线程、对象 ABI 和负向测试，不要提前结束。

## 11. 测试与交付物

至少运行：

```sh
node --check tools/e2_jni_config_observer.js
node --check tools/e2_config_replace.js
python -m pytest tools/tests/test_e2_gate.py tools/tests/test_e2_config_store.py \
  tools/tests/test_e2_observer.py tools/tests/test_e2_public_summary.py -q
python tools/run_main_engine_checks.py
python tools/run_public_checks.py
```

并新增真实测试：

- 配置语义矩阵；
- `PN_POPUP` 状态/持久化矩阵；
- 对象 ABI 和引用生命周期；
- fallback 来源归因；
- original/shadow/replacement/strict-targeted；
- 负向缺键、错值、错路由、错误码和错误生命周期；
- strict gate 真实附件突变拒收。

必须提交：

- `agent-handoff/E2_1_CONFIGURATION_SEMANTICS_2026-10-27.md`
- `agent-handoff/CODEX_REPLY_2026-10-27_E2_1.md`
- `agent-handoff/e2_1_exclusion_ledger.json`
- `agent-handoff/e2_1_semantics_matrix.json`
- `agent-handoff/e2_1_comparison_manifest.json`
- `agent-handoff/e2_1_gate.json`
- `agent-handoff/e2_1_failure_index.json`

报告必须分开写：

- 已通过；
- 已实现但未达到 G-E2.1；
- 未覆盖；
- unknown；
- 环境/私有附件阻塞；
- 下一条最小可执行实验。

## 12. 最终回执

最终回执先写结论，再写证据：

1. 当前提交、分支和 main 身份；
2. 46 个排除键的分类和 fallback 统计；
3. `PN_POPUP` 真实语义矩阵；
4. 默认、异常、重复初始化、持久化、close/free 和引用生命周期结果；
5. 初始化对象契约结果；
6. original/shadow/replacement/strict-targeted 对照；
7. snapshot、actions、restore、reset、destroy 结果；
8. strict gate 的真实退出码；
9. no-VM 是否只到 preinit、首个 tick 还是完整生命周期；
10. 未覆盖项和下一步。

现在开始执行 E2.1。先建立排除键账本和 `PN_POPUP` 矩阵，再逐组扩大 replacement；不要把第一个失败当作整轮停止条件。
