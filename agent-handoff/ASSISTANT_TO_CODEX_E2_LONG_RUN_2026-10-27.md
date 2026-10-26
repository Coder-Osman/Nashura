# E2 长时执行任务书：替换 JavaVM / 配置 JNI 依赖

> 目标分支：`codex/headless-engine-plan-20261025`
>
> 起点提交：`6ddbcc7967d1fc05d5473224b33c2a02bcdc505b`
>
> 任务性质：这是可执行任务，不是方案讨论。请从当前分支继续实现、运行、验证并提交证据。

## 1. 总目标

在保留 E1 只读资源 Provider 和已验证行为的前提下，继续推进 E2：

1. 查清固定战斗轨迹中 JavaVM、JNI、配置桥的完整动态依赖边界；
2. 为战斗必需的配置/初始化语义实现 native replacement 或 host-side shim；
3. 逐步减少并最终切断战斗路径对 JavaVM / 配置 JNI 的依赖；
4. 用原版与替换版的真实原生运行、逐字节快照和响应对照证明行为不变；
5. 在证据足够时，再单独验证“无 VM”与“无 Android 框架”运行。不要把 E2 的局部替换写成已经完成 headless。

本任务不启动正式训练，不推进 G0/G1，不修改 `main`，不修改冻结部署，不把历史失败覆盖成成功。

## 2. 必须先读取的材料

先读取并引用以下材料的实际内容：

- `AGENTS.md`
- `agent-handoff/E1_FMOD_NAMESPACE_VALIDATION_2026-10-26.md`
- `agent-handoff/CODEX_REPLY_2026-10-26_E1.md`
- `agent-handoff/E0_DYNAMIC_DEPENDENCY_COUNTS_2026-10-26.md`
- `agent-handoff/NATIVE_BATTLE_DEPENDENCY_CLOSURE_2026-10-26.md`
- `agent-handoff/NATIVE_BATTLE_DEPENDENCY_CLOSURE_CONTINUATION_2026-10-26.md`
- 当前 E1 Provider、observer、snapshot verifier、原生运行器及其测试
- 现有 `tools/HEADLESS_NATIVE.md` 与已有 headless/QEMU user-mode 证据

开始时记录：

```sh
git status --short
git branch --show-current
git rev-parse HEAD
git show --stat --oneline 6ddbcc7967d1fc05d5473224b33c2a02bcdc505b
```

如果当前 HEAD 不是上述起点，先记录真实 HEAD，确认没有覆盖他人改动，再从实际最新 E1 提交继续。

## 3. 不可违反的范围与证据规则

- 只在 `codex/headless-engine-plan-20261025` 或从该提交派生的任务分支工作。
- 严禁直接写 `main`、训练后端、冻结任务、G0/G1 运行目录。
- 不修改原始 ELF、APK、libg、libfmod、probe、update 文件或其身份摘要。
- E1 的 Provider、FMOD 六槽、libg 九槽、namespace/fallback 语义和 3602 份快照证据必须保留。
- 不以“没有观察到”推导“依赖不存在”。所有零计数都要注明观测边界、线程、阶段、caller 和未配置目标。
- 不把工程 fixture 测试、模拟 JNI 测试或报告生成测试当成真实原生运行证据。
- 不因单次失败、超时、缺失私有附件或一条命令非零就停止整个任务；记录失败并继续可独立的调查、实现和测试。
- 不伪造成功，不删除失败证据，不覆盖旧报告；每次尝试使用独立、不可变、带时间戳的输出目录。
- 未完成严格门前，不宣布“无 JavaVM”“无 Android”“原版引擎已脱离 Android”或“10 倍性能”。

## 4. 长时运行协议

使用当前环境允许的最大执行窗口，持续推进到所有可执行轨道都完成。不要在第一个问题出现后主动结束。

具体要求：

1. 长测试、原生对局、静态扫描和矩阵对照让其自然运行完成；不要因为运行时间长而提前杀掉。
2. 单项失败后先保存命令、退出码、stdout/stderr、环境摘要和已生成文件，再进入下一条独立轨道。
3. 对同一问题至少尝试一种低风险替代路径，例如更小输入、分段运行、已有成功 harness、静态验证或独立 fixture；不得绕过验证。
4. 每个主要阶段结束都写 checkpoint；若执行环境中断，下一次从最近 checkpoint 继续，不从头覆盖。
5. 若所有计划项提前完成且仍有运行时间，继续做独立重复运行、线程/阶段交叉核验、失败语义矩阵和静态闭包审计。
6. 只有以下情况可以结束全部任务：证据已经完成并归档；或遇到会破坏原始身份/数据的不可逆风险；或主机/私有附件确实不可用且所有可独立项目均已完成。即使结束，也必须提交完整的 BLOCKED 报告和下一步。

## 5. E2 阶段 A：JavaVM/JNI 动态清单

先不改语义，建立新的观测版本，至少记录：

- JavaVM 获取、附着、分离、线程归属、调用阶段和调用者；
- JNI 方法名/索引、对象/全局引用生命周期、字符串和数组转换、异常检查；
- 配置桥的 key、类型、默认值、来源、读取次数、首次读取阶段和最后读取阶段；
- Mainloop、初始化线程、控制线程、FMOD 线程和其他后台线程的来源；
- 每条调用是否发生在 cold start、load、battle tick、reset、destroy 或 teardown；
- 原始返回值、错误码、异常状态、输出字节或字符串的摘要；
- 尚未配置的间接目标和 E0 两个短 stub，保持 unknown，不要补造计数。

输出一份结构化清单（JSON 或 CSV）和可读摘要。清单必须能区分“战斗热路径必需”“只在初始化出现”“后台线程”“未归因”“未覆盖”。

## 6. E2 阶段 B：设计并实现 replacement

优先实现最小、可验证的 native 配置接口，不要一开始重写整个 Java 层：

- 从真实动态清单提取固定战斗轨迹所需的配置集合；
- 设计明确的类型、默认值、namespace、错误和缺失语义；
- 对字符串、数组、对象引用和异常路径分别定义生命周期；
- 保留一个可观测的 fallback，仅用于尚未覆盖的调用；每次 fallback 都必须计数并带 caller/阶段；
- 对已覆盖调用提供 native 返回值，并能切换 original / replacement / fallback 三种模式；
- replacement 不得依赖 JavaVM、JNIEnv、Android Context 或隐式全局状态；
- 线程安全、reset/reinit、destroy 后访问和重复初始化必须有明确语义；
- 不能只在测试 fixture 中替换，必须接入真实引擎入口。

若某个 JNI 调用无法安全替换，保留它并把理由、输入输出、阻塞条件和下一步写入报告，不要伪造“已切断”。

## 7. E2 阶段 C：渐进式真实验证

至少完成以下独立模式，并分别保存完整证据：

1. **original**：原始 JavaVM/JNI 配置链；
2. **shadow**：原始返回值与 replacement 返回值并行计算，只比较不改行为；
3. **replacement**：已覆盖调用由 native replacement 提供，未覆盖调用显式 fallback；
4. **strict-targeted**：对已声明的战斗配置闭包禁止 fallback，任何遗漏立即失败；
5. **no-VM probe（仅在前四步稳定后）**：从真实 native 入口启动，不创建 JavaVM；若失败，保留失败原因，不把失败改写成通过。

每种模式至少比较：

- 冷启动至终局；
- 3601 ticks / 28 actions / 3602 snapshots；
- snapshot/restore 分支；
- terminal reset 和 destroy；
- 线程归属与调用阶段；
- 原生快照逐字节、长度、SHA256；
- 动作回执、错误码、文件/配置返回值；
- Provider 操作、AssetManager 事件、JavaVM/JNI 事件、fallback 次数、live handle 数。

优先做 3 次以上相互独立的 replacement 运行；若资源和时间允许，再做不同进程、不同线程调度和重复 reset。

## 8. E2 严格门

只有全部满足才可写 “E2 targeted strict gate passed”：

- 已声明的战斗配置调用集合有来源、类型、线程、阶段和返回值证据；
- strict-targeted 运行中 JavaVM/JNI/config fallback 为 0，或每个非零项已被明确列为未覆盖并不纳入门；
- original / replacement 的 3602 份原生快照逐字节一致；
- actions、终局、restore、reset、destroy 一致；
- 关键异常、缺失、默认值和重复初始化语义一致；
- 至少一次独立进程复跑；
- 所有负向篡改/遗漏测试能阻止伪造通过；
- 原始输入和私有运行身份 SHA 前后一致。

“no-VM passed”需要另外的门：真实进程在不创建 JavaVM 的情况下完成同样的原生生命周期，并且没有通过隐藏 Android/Java fallback 作弊。只要没有完成，不得使用该表述。

“Android-free”需要另外的门：对 Android linker、AssetManager、系统文件和其他平台调用进行独立清单和替换验证。E2 不自动等于 Android-free。

## 9. E1 残余覆盖（在 E2 主线阻塞时并行推进）

如果 E2 的某条私有依赖暂不可用，不要空等，继续完成以下可独立工作：

- 自然 FMOD 音频资源 open/read/seek/close 的真实播放路径；
- OS 进程 teardown 的资源和线程回收；
- E0 两个失败 hook、未配置间接目标的静态/动态来源归因；
- 现有 QEMU/user-mode headless 入口与 E2 replacement 的组合验证；
- 观测器开销优化和至少三次低开销重复运行。

这些结果必须与 E2 结果分开，不得混成一个“已完全闭包”的结论。

## 10. 测试与交付物

运行与改动相称的检查，至少包括：

- 新增/修改 C/C++ 编译：`-Wall -Wextra -Werror`；
- 新增/修改 JS：`node --check`；
- E0/E1 相关 pytest；
- `python tools/run_main_engine_checks.py`；
- `python tools/run_public_checks.py`；
- 真实原生 original/shadow/replacement/strict-targeted 对照；
- `verify_e2_gate.py`（若没有则实现，必须拒绝真实证据的负向突变）；
- identity、listener、adb/AVD/QEMU 实例回收检查。

提交以下文档和机器可读证据：

- `agent-handoff/E2_JAVAVM_CONFIG_JNI_REPLACEMENT_2026-10-27.md`
- `agent-handoff/CODEX_REPLY_2026-10-27_E2.md`
- `agent-handoff/e2_dynamic_dependency_counts.json`
- `agent-handoff/e2_comparison_manifest.json`
- `agent-handoff/e2_gate.json`（仅在验证器真实通过时写入 passed）
- 失败与阻塞证据索引，保留每次尝试的退出码和输出目录
- 如有源码改动，附变更摘要、测试命令、身份 SHA 和回退说明

报告必须明确分开：

- 已通过；
- 已实现但未达到严格门；
- 未覆盖；
- 被环境/私有附件阻塞；
- 下一步最小可执行动作。

## 11. 最终回复格式

最终先给结论，再给证据：

1. 当前提交和工作分支；
2. 实际改动文件；
3. JavaVM/JNI/config 调用闭包与剩余未知项；
4. original/shadow/replacement/strict-targeted/no-VM 各自结果；
5. 快照、动作、reset/destroy 和线程证据；
6. 测试命令与退出码；
7. 失败尝试、绕行路径及仍然阻塞的原因；
8. 是否达到 E2 targeted strict gate；
9. 是否达到 no-VM；
10. 是否达到 Android-free；
11. 下一步。

不要只给截图、简短 status 或“测试通过”一句话。所有结论必须能回到提交、报告、机器可读证据和实际命令。

现在开始执行。先做阶段 A 的完整清单，再逐步实现；遇到问题记录后继续，不要在第一个问题处停下。
