# E2.1 配置语义续验（2026-10-27）

## 结论与身份

**G-E2.1：BLOCKED，实际 CLI 退出码 2。** 46 个排除键账本及限定原生对照已完成；
这些键仍全部保守分类为 `required`，尚未新增到 native replacement。
PN_POPUP 的原桥行为取得新证据，但持久化提交、并发、异常及配置对象释放契约仍未知。
不以快照相同替代配置 ABI 等价，不进入 G-E2.2 或 G-E2.3。

- 工作分支：`codex/headless-engine-plan-20261025`。
- 本轮代码基线：`7c601300eb425b82981f8c78f0e4b2accb692490`。
- 任务书历史基线：`db81efceb99b1b46e12d1d2b14c4f89a344daad4`。
- 核验的 main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`；保持原样。
- 本报告与代码的交付 commit 由包含它们的任务分支提交确定，见 Git 历史；不使用自引用 SHA。
- 宿主：Darwin ARM64、macOS 15.6、36 GiB 内存；实验使用新独占 Android AVD。

APK、libg、FMOD、probe、Provider、index、990 个 update 文件在每个成功 run 前后身份相同。
libg SHA256 为 `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`，
APK 为 `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b`。
私有 manifest 绑定 36,418 个附件；公开摘要只保留身份和长度/摘要，不发布配置值、
原始快照、原始审计、第三方二进制或私有路径。公开附件不能独立重建原生实验。

## 已通过的限定原生对照

| 独立进程目录标识 | 模式与目的 | 实际退出码 |
|---|---|---:|
| original-1 | 无新增语义 probe 的原桥基线 | 0 |
| matrix-2-original | 原桥语义探针 | 0 |
| matrix-3-shadow | 32 键 shadow，PN 保留原桥 | 0 |
| matrix-4-replacement | 32 键 native，PN 保留原桥 | 0 |
| matrix-5-strict-targeted | 未知键真实负向，按预期 abort | 1 |
| matrix-6-strict-lifecycle | 正常 strict、JNI 检查、原生字符串析构 | 0 |
| matrix-7-prepare-empty | 顶层 exists/write/read/remove；准备 PN 空值 | 0 |
| matrix-8-empty-cold | 空值冷启动；准备 malformed | 0 |
| matrix-9-malformed-cold | malformed 冷启动；删除 PN | 0 |
| matrix-10-missing-cold | 缺键冷启动；恢复保存值 | 0 |

九次成功 run 均完成 tick 3601 原生终局、28 个固定动作、3,602 个原生快照，
9,120,440 bytes 全字节一致，无字段归一化。轨迹 SHA256：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
17 个排除 status 轮询后的控制响应一致，包括 configure、21 tick 快照、restore、
release、reset、终局后 reset。涉及战斗状态的销毁/重建不证明配置对象析构或 OS teardown。
独立 decoded legal masks/rewards/events 的逐字段语义校验未新增；本轮证明的是序列化字节及控制响应。

E1 正常资源观测继续为平台 AssetManager 事件 0、libg 九槽及 FMOD 六槽覆盖、
Provider failures=0、live_assets=0；不将它外推到未观测调用目标。

## 46 键账本与 fallback 归因

`e2_1_exclusion_ledger.json` 区分 46 个唯一键和 99 次历史自然 fallback。
每键保存操作、首次/末次阶段、线程、caller、输出字节摘要、状态、证据序号与分类依据。
初始化仍可达且尚无移除证明，因此 46 项均为 `required`，不是已证实可移除项。
原桥输出可证明 native 字符串表示，Java 存储类型、逐事件 JNI 栈关联仍可能未知。

历史 replacement 在返回原桥前记录 fallback；该记录没有原桥返回值时明确 `unknown`。
original 与 replacement 来自不同进程，账本只按键关联，不拼接两个进程的 sequence、
返回值或调用栈。JNI 参数每 bucket 只保存前三条，调用计数继续累加；未采样参数
不写成全覆盖。配置事件和错误没有丢失。两个短 stub hook 仍失败，count=null。

本轮 shadow/replacement 各有 32 次目标读、4 次目标写、目标 fallback=0。
总 fallback=116，其中 99 次自然路径、16 次 PN 原桥探针、1 次显式缺键探针。
正常 strict 为 115（不含显式缺键探针）。显式探针只在配置声明、真实结果和线程对应后
归为 probe-induced；其它未知键仍拒收，不被计数过滤掩盖。

## PN_POPUP：原桥矩阵和边界

PN 在 original、shadow、replacement、strict 中始终走原 JNI，未实现 PN native store。

| 场景 | 本轮实际观察 | 证据边界 |
|---|---|---|
| 正常冷启动与读回 | 原引擎自然读/写，探针可读回 | 保留原 JavaVM/初始化顺序 |
| 同进程重复读、reset、终局 reset | 读值保持 | 未重复 ConfigurationConstructor |
| 空串、malformed、UTF-8、0/4095 bytes | 原桥写读字节一致，恢复保存值 | 4095 是探针界限，不是引擎最大值 |
| 空值后的新进程 | 自然读取准备值，再写回有效计数 | 未定位 commit/apply 或同步异步时机 |
| malformed 后的新进程 | 自然读取 malformed，再写回有效计数 | 不外推其它非法格式 |
| 缺键后的新进程 | exists=false，原桥读空，冷启动写回有效计数 | 原生默认值规则仍未迁移 |
| 顶层临时 key | exists false→write→exists true→top/raw read 相同→remove→exists false | 原生入口诱发的有界探针，不是自然游戏请求 |
| JNI 异常状态 | 后期正常 probe 线程 ExceptionCheck 前后均 false | 未触发异常，未验证清理和其它线程 |

真实入口包括 readBridge `0x140c68c`、writeBridge `0x140b7b4`、
PreferenceRead `0x139412c`、Exists `0x13941b0`、Write `0x139429c`、Remove `0x139423c`。
读取通过 AArch64 x8 返回 EngineString16；写和删除按 void 处理，不把寄存器内容猜成成功码。
缺键、空值可在 exists 上区分；本轮不把它们合并成统一空值成功语义。

低风险补验采用同一独占实例的多个 fresh process 顺序链，而非在未知所有权条件下
重复构造配置单例或访问已释放对象。四进程链保存自然 read/write 顺序、线程及字节身份，
最后恢复原 PN 值。该路径补足原桥跨进程可见性，但不证明 native 持久化等价。

## 字符串与引用生命周期

在真实 libg 上构造并析构七个自有 EngineString16，ctor `0x138a810`、dtor `0x138a700`。
只读 hook 真实 libc.free，并只匹配本探针拥有的 heap 地址；不读取释放后对象。

| UTF-8 字节长度 | 存储 | 每个对象实际 free |
|---:|---|---:|
| 0、1、7 | inline | 0 |
| 8 | heap | 1 |
| 7（多字节文本） | inline | 0 |
| 10（多字节文本） | heap | 1 |
| 4095 | heap | 1 |

构造字节、尾部 NUL、长度及上述释放次数通过；七个对象不等于 Java local/global 引用退休、
配置 store close/free 或异常清理已通过。语义桥探针的其它临时对象采用有界进程生命周期持有，
未声称它们被逐项析构。MUTF-8、数组、对象和嵌入 NUL 等仍未覆盖。

## 初始化对象契约

严格 run 的实际入口、caller、线程、first_sequence、phase 记录在
`e2_1_comparison_manifest.json`，不根据静态名字猜对象语义。
FileSystemBootstrap、ProviderBootstrap、APKProviderRegister、ReaderRegister、SCDBOpen、
GameConfigurationInit、GamePreinit、GamePreinitConfigure、ContentBootstrap、StateMaskStage、
IDRegistration 各观察到一次；TableLoadBatch 五次，GameStateConstructor 三次。

入口顺序和 caller 是已观测项。每层对象大小、对齐、字段布局、参数所有权、失败清理、
跨线程可见性及配置 store 释放均未闭合，因此未构造 no-VM 替代初始化链。
两个短 stub 保持 `uncovered`，不扩函数边界或将 hook 失败改写成零调用。

## 门禁、负向与实现范围

新增账本构建器、显式可选的语义/生命周期 probe、真实附件验证器、脱敏导出及测试。
旧运行入口在未配置新 probe 时维持旧行为。没有扩大 replacement 表或跳过初始化。

`verify_e2_1_gate.py` 校验附件 SHA、输入身份、各 run 独立进程和源码身份、完整快照、
控制响应、资源调用、32 键路由/值/状态/计数、冷启动链及字符串释放事件。
当前验证器只认证这些限定证据，并对未闭合项固定 fail closed；它不是未来完整语义的通用 PASS 验证器。

真实 strict 未知键负向在完整轨迹之后触发 `e2_contract_violation`，进程退出 1。
已保留 caller/线程/阶段/消息与失败记录；不计作成功 run。
附件突变测试覆盖错值、路由、状态、归因、fallback、计数、字节、异常、reset、free、
缺失事件、冷启动输入/写回/顺序/exists/恢复及摘要身份拒收。
`e2_1_failure_index.json` 记录调试失败摘要和原始附件 digest；旧 E0/E1/E2 失败归档保持原样。

## 实际命令和结果

以下 `$PY` 为本机已有项目 Python，`$RUN` 为独占私有实验根，`$OLD` 为只读旧证据根。
命令在任务工作树运行。私有值和机器路径不随报告发布。

```sh
E2_1_PRIVATE_MANIFEST="$RUN/e2-1-private-manifest-v3.json" "$PY" -m pytest \
  tools/tests/test_e2_1_gate.py tools/tests/test_e2_1_ledger.py \
  tools/tests/test_e2_1_public_summary.py -q
# exit 0：33 passed in 9.13s，使用真实私有附件

E2_PRIVATE_MANIFEST="$OLD/e2-private-manifest-v3.json" "$PY" -m pytest \
  tools/tests/test_e2_1_ledger.py tools/tests/test_e2_gate.py \
  tools/tests/test_e2_config_store.py tools/tests/test_e2_observer.py \
  tools/tests/test_e2_public_summary.py -q
# exit 0：37 passed in 8.67s

"$PY" tools/verify_e2_1_gate.py \
  --manifest "$RUN/e2-1-private-manifest-v3.json" --output "$RUN/gate-attempt-5.json"
# 实际 exit 2：bounded_native_comparisons=passed，G-E2.1=blocked
# output 为独占新文件；重验应另选不存在的输出名

"$PY" tools/e2_1_public_summary.py \
  --manifest "$RUN/e2-1-private-manifest-v3.json" --gate "$RUN/gate-attempt-5.json" \
  --output-directory agent-handoff
# exit 0：三份脱敏 JSON；不可覆盖已有输出

node --check tools/e2_jni_config_observer.js
node --check tools/e2_config_replace.js
node --check tools/e2_1_config_probe.js
node --check tools/e2_1_string_lifecycle.js
# 四项均 exit 0

"$PY" tools/run_main_engine_checks.py
# exit 0：211 passed in 32.36s

"$PY" tools/run_public_checks.py
# exit 0：主组794 passed/27 skipped；八组专项见下
```

八组专项依次为 23、82、15/3 skipped、25/4 skipped、31/3 skipped、31/2 skipped、
38/3 skipped、65 passed；集合有重叠，不相加。初次缺少私有变量的 15 passed/22 skipped
是工程结果，不计为原生验收。最终 source-manifest、gitleaks、diff 检查另记在交付回执。
首次凭据扫描 exit1 的三处告警均为账本中的静态键名 `Pass_INTEGRATION4`，没有配置值。
只为这个完整字面量登记精确误报豁免，不放宽其它规则；原始扫描记录私有保留。

## 未覆盖、unknown 与下一条实验

未覆盖项：真正的重复配置初始化、异常触发及清理、并发读写、只读写入错误、
未初始化读取、超界值、配置 store close/free、Java 引用退休、数组/对象/MUTF-8、
初始化各层完整 ABI。现有环境和合法输入足以完成本轮限定实验；阻塞是语义证据和实现，
不是把工具安装或单测通过当作完成。

下一条最小实验仍限定 E2.1：新建独占 Android oracle，仅沿 PN 写桥定位真实 Java 配置方法及
commit/apply 路径；记录返回时刻、同步/异步提交、fresh-process 可见性，再做两个受控线程
的交错读写与异常观测。先得到原桥契约，再设计 typed errors、线程与 close 的 native ABI，
随后独立跑 original/shadow/replacement/strict 四模式；不提前实现猜测默认值。

本轮独占 engine、Frida、AVD、adb、转发及监听器已回收，监听残留 0；历史 adb 身份不变。
克隆与失败证据保留。未运行新 no-VM 探针、正式训练、多机放量或吞吐实验。
