# E2：配置 JNI 局部替换与无 VM preinit 诊断

## 1. 结论与验收层级

任务分支 `codex/headless-engine-plan-20261025`；起点
`d812102edf724807249e65942da6fcf52334fd01`，包含用户 E2 长时任务书。
代码提交身份以包含本报告的提交为准，可用 `git log -1 --format=%H -- tools/verify_e2_gate.py` 查询。
main/origin/main 保持 `0012201830cfbfcbfeb3d9dccd91a855028da869`。

**已验证：32 个明确声明的配置键，在固定完整原生轨迹内可以改走 native store，
而 3,602 份快照、28 个动作、终局、restore、reset 与销毁响应保持一致。**
**尚未达到任务书的 E2 targeted strict gate；无 VM 完整局和 Android-free 均未通过。**

| 层级 | 结果 | 依据与限制 |
|---|---|---|
| 32 键固定轨迹替换 | PASS | original/shadow/3 次 replacement/2 次 strict-targeted，真实原生字节对照 |
| E2 targeted strict gate | BLOCKED | 原生默认/异常/重复配置初始化/持久化及 store 关闭释放语义尚缺等价证据 |
| 全部配置 JNI 闭包 | BLOCKED | 46 个逐项排除键继续走原路径，每次完整替换运行有 99 次显式 fallback |
| 无 VM preinit 局部探针 | 原入口已返回 | 独立诊断加入第 33 键；尚未按完整原初始化顺序建立战斗状态 |
| no-VM 完整局 | BLOCKED | 未完成原生创建、动作、tick、终局及快照全生命周期 |
| Android-free / 10× / 训练准入 | 未验证 / 未运行 / false | 保留 Android linker/Bionic/framework；未启动训练或放量 |

早期私有 `e2-gate-v1/v2/v3.json` 的 passed 仅代表当时的 32 键固定轨迹检查，
不具备任务书严格门的全部语义条件。最终验证器默认 strict，真实 CLI 返回 2；
显式 `--scope fixed-trajectory` 返回 0，并始终标记 `targeted_strict_gate_verified=false`。
历史证据原样保留，不回写历史结果或缩小任务书门槛。

## 2. 输入、环境与保护边界

本机 Darwin/ARM64，使用本轮独占 Android ARM64 AVD `e2-original`。
保留既有 Java VM 和应用初始化顺序做完整局对照；未修改原始 APK、libg、FMOD、probe 或 update。
既有 QEMU user-mode 历史结果仅作背景，本轮没有执行 Linux ELF 或用模拟器原型替代原生证据。

| 输入 | SHA256 |
|---|---|
| libg | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 历史离线包装 APK | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| probe | `84cddf82e866540ff33d4b5f0c6b5b535c8485182e4cc84961028c7a56e65ec4` |
| FMOD | `299d2a6b03ec865cd9234f4f7484fb7e74928d0180dc6c4b455a8d2b5b54dd45` |
| E1 Provider | `82732f436560055e60a8f2e760b731bafd8436c02313cf086a109d55476f234e` |
| E1 APK index | `6c9b427faeaba3a83c487754637be0eca7c4ab43516f660e2de813043babd1ed` |

包装 APK 与 supported_engine 的原包不是同一文件；仅声明匹配原版 libg 指纹。
990 个 update 文件及完整输入在各成功运行前后核验；摘要见 `e2_comparison_manifest.json`。
原始配置值、Java 返回值、设备路径、原始汇编、二进制及私有审计全部留在独立私有目录。
公开 JSON 是白名单聚合，不足以单独重验原生结果；重验需提供原始私有附件。

## 3. 实现与数据契约

- `tools/e2_jni_config_observer.js`：JavaVM/JNIEnv 表、配置桥、字符串/数组/引用操作计数，
  按线程 native scope 与宿主时间窗区分阶段。相同函数指针别名只计一次，歧义保持 unknown。
- 区分 engine 16-byte String 与 libc++ 24-byte String，修正 top-byte-tag 指针读取、
  MUTF-8 与 UTF-16 长度、输出 buffer 的 onLeave 采样；保存真实返回样本和丢弃样本数量。
- `tools/e2_config_store.c`：显式 context、pthread 锁、128 键上限、4095-byte 值上限，
  create/add/seal/read/write/close/free。未知键、只读写入、未初始化、关闭后访问均有明确错误码。
  不依赖 JNI；不定义隐式默认值或跨进程持久化。free 前要求调用方停止并发访问。
- `tools/e2_config_replace.js`：仅重定向原生读写桥，保留原 PreferenceRead/Exists 缓存及锁。
  自有 ARM64 trampoline 保留 x8 aggregate return，调用原字符串构造器，不伪造 allocator。
  原函数指针、caller、线程、阶段、native scope、路由及 fallback 均留证。
- 未知键只在明确排除清单内走原路径；strict-targeted 未声明调用和 shadow 不一致直接 abort。
  早期在 JS 回调调用 abort 被 Frida 转为异常的失败尝试保留；最终从 native stub 直接 abort。
- `run_e2_observed.py` / `run_e2_series.py`：身份核验、独立输出、完整生命周期、
  三阶段 `/proc/PID/task/TID/comm` 线程名采集、仅清理核验过 PID/启动时间的所属实例。
  结束回执在证据快照全部落盘后才写；验证器额外拒绝 evidence-incomplete 和 Frida error。
- `verify_e2_gate.py`：哈希绑定附件、原生快照逐字节检查、17 个控制响应比较、
  E1 资源覆盖检查、排除键来源关联、计数交叉核验、不同 PID 复跑和真实负向实验。
- `summarize_e2_evidence.py`：白名单脱敏，仅输出计数、阶段、线程类别和 SHA。

32 键均来自原版 ConfigurationConstructor 的真实返回；28 个返回空字符串，4 个为非空且可写。
这不是统一返回空串。四个幂等写是音效/音乐开关及音量；真实轨迹每次读 32、写 4。
原版完整观测共 396 条配置行，读取键 77 个、读写合计 78 个。
`PN_POPUP` 每次冷启动递增，排除出稳定 32 键；其余排除键也逐项记录原始 sequence 和原因。
正式整局 replacement/strict-targeted 的 targeted fallback=0、native store failures=0，
但 outside fallback=99。不得将其描述为 JNI 全零。

## 4. 原生对照结果

规范证据集：original-v11；targeted-v3-1-shadow；targeted-v3-2/3/4-replacement；
targeted-v2-1/2-strict-targeted。每项均是独立进程。

| 模式 | 原生结果 | VM/JNI 总计（观测范围内） |
|---|---|---|
| original-v11 | 完整轨迹通过 | 11,331 / 44,135 |
| shadow | 完整轨迹，32 读/4 写比较一致 | 10,391 / 42,006 |
| replacement 第 1/2/3 次 | 全部完整轨迹通过 | 9,985/39,346；10,224/40,039；10,182/40,334 |
| strict-targeted 第 1/2 次 | 全部完整轨迹；目标无 fallback | 11,566/45,105；11,446/44,647 |

每局 terminal tick=3601、actions=28、snapshots=3602、总快照 9,120,440 bytes。
统一轨迹 SHA256：`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
21 tick snapshot/restore 分支重演一致；终局、reset、GameState destroy 有真实响应/计数证据。
17 个非 status 控制回执完全匹配，仅去除输出目录路径；快照字节不做字段归一化。

E1 保持：libg 九槽、FMOD 六槽已验证；正常生命周期实际 AAsset 平台调用 0；
Provider 19,474 次、failures=0、live_assets=0。这是规定观测生命周期，不含所有自然音频播放。

最终源码另做 final-baseline、final-runner 和 thread-audit 原版/strict 两次，共四次补验；
全部与 original-v11 的 3,602 份快照、控制响应、资源门和前后身份一致。
补验不冒作旧源码身份，不替换排除键原始 sequence；源码副本随私有 run 归档。

## 5. 线程、计数与未覆盖项

最终线程补验 original：JNI 40,336，VM 9,432；strict-targeted：JNI 39,447，VM 9,774。
两次分别观测到 Mainloop 命名线程 JNI 29,814 / 28,870，FMOD 命名线程各 50。
线程名只在冷 ready、轨迹前、轨迹后三个时点读取；早退/改名线程继续 unknown，
不凭线程名证明调用因果。旧 run 的 ps NAME 实为进程名，未用它伪造线程分类。

已配置 JNI/VM hook 在同线程 Step scope 内计数为 0；跨线程 full_trace 窗口仍有调用。
零只代表该 hook 范围与该固定轨迹，未配置目标、内部 resolver、后台调用不据此排除。
配置替换使阶段分布明显变化，后台调度/插桩耗时也变化，聚合次数差不是依赖消除比例或性能结论。

观测器保持全量配置行（dropped config=0），但 JNI 返回值限量采样：
original-v11 省略 40,967 个重复返回样本，线程补验分别省略 37,489 / 36,774。
操作计数保留，不声称全部返回值已解释。Java class raw handle 重用使部分类名未知。
引用创建/删除只有操作计数，local frame 隐式退休和 handle 重用未闭合，live JNI ref 未知。

E0 两个失败 hook 为短尾跳入口，边界不足；本地静态审计保留，未扩函数范围或补造动态计数。
自然 FMOD 音频 open/read/seek/close、OS teardown 的语义级回收、一般配置异常及隐式默认仍未覆盖。

## 6. No-VM 探针与最小剩余依赖

独立 native shell 进程只 dlopen 匹配 libg；不创建应用进程或 JavaVM。
仍在 Android guest 内使用 linker/Bionic，libandroid_runtime 已映射；libart 未映射、
引擎 VM 指针为 null。JNI_GetCreatedJavaVMs 在无 libart 时不可查询，返回状态保留 -1，
未伪造 0 个 VM 的成功 API 回执。

- v1：加载期间附加触发 linker/Frida 失败，记录保留。
- v2–v5：早期资格检查把 framework 映射误当 VM，退出；修正判断后另起新目录。
- v6：稳定 32 键替换后，PN_POPUP 的真实 fallback 导致空 VM 解引用崩溃。
- v7：仅诊断加入 PN_POPUP 的上一真实写回值及 SHA/sequence，观察到 33 次读、4 次写、
  fallback=0，原 preinit 返回。native probe 设计返回 5；host 返回 2 表示完整战斗仍 BLOCKED。
  Frida detach 记录不是 guest 退出码，未将设计值伪称实测进程码。

v7 是单入口最小探针，未沿完整冷启动顺序创建状态，不纳入 32 键完整局门。
旧 resource_probe 含诊断性 flag 写入和 graphics 跳过，未拿来冒充严格冷启动。
实际顺序待逐项组合：FileSystemBootstrap → ProviderBootstrap → APKProviderRegister →
ReaderRegister → SCDBOpen → GameConfigurationInit → GamePreinit/Configure →
ContentBootstrap → TableLoadBatch → StateMasks/IDs。

最小后续动作：按真实观测补齐每个入口的对象/参数/所有权证据，先验证
文件系统、SCDB 与内容初始化，再进入 GameState；同时把 PN_POPUP 写入/持久化及其余
排除键做真实缺失/默认/重复初始化矩阵。未知键继续失败，原顺序及输入身份不变。

## 7. 实际命令和测试结果

下列环境变量代表本机既有私有输入，精确展开 argv、退出码与命令日志保存在私有 run。
不发布包含设备路径和资源值的配置，也不声称这些变量可在干净仓库凭空复现。

```sh
"$PY" tools/run_e2_series.py --config "$E2_CONFIG" --root "$E2_ROOT" \
  --previous "$PREVIOUS_OWNED_RUN" --modes shadow replacement replacement replacement --prefix targeted-v3
# 四次退出0；strict-targeted 两次由 targeted-v2 独立完成，均退出0。
"$PY" tools/run_e2_series.py --config "$E2_CONFIG" --root "$E2_ROOT" \
  --previous "$E2_ROOT/final-runner-1-strict-targeted" --modes original strict-targeted --prefix thread-audit
# 两次退出0，新增真实线程名证据。
"$PY" tools/verify_e2_gate.py --manifest "$E2_ROOT/e2-private-manifest-v3.json" \
  --output "$E2_ROOT/e2-gate-final-strict.json"
# exit2：固定轨迹通过，但严格语义门BLOCKED。
"$PY" tools/verify_e2_gate.py --manifest "$E2_ROOT/e2-private-manifest-v3.json" \
  --scope fixed-trajectory --output "$E2_ROOT/e2-gate-final-fixed-trajectory.json"
# exit0：仅32键固定轨迹；targeted_strict_gate_verified=false。
E2_PRIVATE_MANIFEST="$E2_ROOT/e2-private-manifest-v3.json" "$PY" -m pytest \
  tools/tests/test_e2_gate.py tools/tests/test_e2_config_store.py \
  tools/tests/test_e2_observer.py tools/tests/test_e2_public_summary.py -q
# 32 passed，6.29s，exit0。含真实附件/快照负向突变，非纯fixture。
"$PY" -m pytest tools/tests/test_e0_observer.py tools/tests/test_e0_snapshot_verifier.py \
  tools/tests/test_e1_engine_probe.py tools/tests/test_e1_gate.py tools/tests/test_e1_resources.py \
  tools/tests/test_e1_semantics.py tools/tests/test_e2_config_store.py \
  tools/tests/test_e2_observer.py tools/tests/test_e2_public_summary.py -q
# 101 passed，7.81s，exit0；集合重叠，不累加。
"$PY" tools/run_main_engine_checks.py
# 211 passed，21.27s，exit0。
"$PY" tools/run_public_checks.py
# exit0；主组794 passed/27 skipped，其余八组通过；skip不等于原生覆盖。
"$NDK_CC" -std=c11 -D_POSIX_C_SOURCE=200809L -Wall -Wextra -Werror -O2 \
  -fPIC -shared -pthread tools/e2_config_store.c -o "$PRIVATE_BUILD/libe2config.so"
"$NDK_CC" -std=c11 -Wall -Wextra -Werror -O2 tools/e2_no_vm_probe.c -ldl -o "$PRIVATE_BUILD/no-vm-probe"
node --check tools/e2_jni_config_observer.js
node --check tools/e2_config_replace.js
node --check tools/tests/e2_observer_harness.js
# 两个Android ARM64构建及三个JS检查均exit0。
```

负向原生：删 SfxEn 与 shadow 错值两次均 abort、runner exit1；真实快照 tick123
改 1 byte、身份/重复进程/漏模式/漏负向、错误回退/值/计数/JNI残留等均被测试拒绝。
测试仅证明相应拒收行为，不补足原生通用配置语义。

## 8. 失败记录、资源回收与交付

`e2_failure_index.json` 保留每次 attempt token、已记录退出码和证据 SHA；
缺少退出码的早期 harness 明确 null/unknown，不补造。original-v7 完整轨迹成功但
外层摘要路径错误，后续独立快照校验成功；v8/shadow-v1、旧 strict、no-VM 失败均保留。

原生实验结束后仅回收本轮 e2-original、专用 adb5051 及所属转发，核验旧 adb5037
PID/启动时间不变。私有回收回执与脱敏 publication evidence 单列；回收不等于 OS teardown
资源语义已验证。未删除任何历史数据、原始输入或失败证据。

本轮交付：本报告、Codex 回执、动态计数、比较清单、严格门状态和失败索引；
必要源码及测试。下一轮从严格语义矩阵和原初始化入口 ABI 验证继续，训练门保持关闭。
