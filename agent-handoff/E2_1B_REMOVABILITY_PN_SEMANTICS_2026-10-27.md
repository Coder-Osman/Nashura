# E2.1-B：排除键可移除性与 PN_POPUP 语义限定续验

## 状态与范围

**E2.1-B 尚未闭合；G-E2.1 仍为 BLOCKED。** 本轮完成五个独立原生进程的
完整固定轨迹、PN 原桥 Java 提交路径及同值双线程观测，未移除任何排除键，也未将
PN 加入 native replacement。新增 bounded 验证器退出 0，仅认证本报告限定的证据；
旧严格门实际重验退出 2。二者不是同一个验收门。

- 分支：`codex/headless-engine-plan-20261025`。
- 工作基线：`5643fb30ebee184b5f990ae5cd9aab14a02c12bb`。
- main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`，保持原样。
- 交付 SHA 为包含本报告的任务分支提交，见回执与 Git 历史；不预填自引用 SHA。
- 本机独占 Android ARM64 克隆实例；JavaVM、原初始化顺序、原配置桥均保留。
- 未进入 E2.2/no-VM、Android-free、训练、G0/G1 放量或 10× 实验。

公开源码和摘要不含第三方二进制、DEX、配置值、原始快照、原始审计或私有路径。
重验原生结论需要合法准备的匹配输入及私有附件；公开单测不替代这些附件。

## 1. 身份、完整轨迹与模式对照

每次运行前后核对 APK、libg、libfmod、probe、Provider、index 及 990 个 update 文件，
与历史参考身份一致。原始输入没有修改。

| 身份 | SHA256 |
|---|---|
| APK | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| libg | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 当前 APK 的 classes.dex | `ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608` |
| 旧分析输入 DEX | `c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6` |

旧 DEX 与当前 APK 的 DEX 不同，本轮静态结论使用当前 APK 提取的匹配 DEX；
不以旧 DEX 的函数内容替代当前版本证据。提取物和反汇编只留私有目录。

| 独立进程标识 | 模式 | 实际 runner 退出码 | 完整轨迹 |
|---|---|---:|---|
| baseline | 原桥基线 | 0 | 一致 |
| series-1-original | 原桥＋显式 Java probe | 0 | 一致 |
| series-2-shadow | 32 键 shadow，PN 原桥 | 0 | 一致 |
| series-3-replacement | 原 32 键 native，PN 原桥 | 0 | 一致 |
| series-4-strict-targeted | 原 32 键 targeted strict，PN 原桥 | 0 | 一致 |

五次均完成 tick 3601 原生终局、28 个固定动作、3,602 个原生快照，
9,120,440 bytes 全字节一致，未归一化字段；与历史参考也一致。
轨迹 SHA256 为 `0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。
snapshot/restore/reset 及非轮询控制响应一致；这不是配置 store 析构或 OS teardown 证明。
E1 平台 AssetManager 事件 0、Provider failures=0、live_assets=0，libg 九槽和 FMOD 六槽
继续通过已有资源证据检查，不外推至未观测间接调用。

shadow/replacement/strict 各 32 次目标读、4 次目标写、targeted fallback=0。
本轮各有 **110 次**原路径 fallback：99 次自然调用＋11 次 PN 显式 probe。
历史 115–116 次采用了不同探针集合；数量减少不是依赖被移除。
JNI 参数采样仍有超限丢弃，五 run 数量分别为 55,770、64,773、64,788、57,904、58,307；
配置事件和错误未丢弃。两个历史短 stub hook 仍未覆盖，不把其调用数写成零。

## 2. 46 个排除键：保留策略与必需性分开

`e2_1b_removability_ledger.json` 对 46 个唯一键逐项保存桥操作、caller、线程、
宿主 phase、sequence 和独立 wrapper 观测。自然桥调用共 99 次（77 读、22 写）；
另有 3 次 baseline 显式 probe，未混入自然计数。wrapper/JNI 是别名观测层，不相加。

| 字段 | 本轮含义 |
|---|---|
| historical_replacement_class | `required`，保留历史路由分类 |
| containment_policy | 保留原路径，避免猜测默认值 |
| battle_necessity | 46 项全部 `unknown` |
| removability | 46 项全部 `unknown` |
| removal_intervention | 全部 `not performed` |

已观测初始化可达性不等于“战斗必需”。宿主 phase 不等于业务消费者；同一固定轨迹
没有观察到调用也不证明可移除。本轮新增 caller/别名层证据，但尚无逐键消费者 ABI、
所有权与移除反事实，因此不宣称已完成可移除性判定，也不扩大替换表。
历史 `e2_1_exclusion_ledger.json` 原字节保留，新账本纠正其解释边界。

## 3. PN：静态发现与运行时观察

### 当前 DEX 的静态发现

以下是匹配当前 APK 的静态发现，尚不等于所有分支都已执行：

- `ApplicationUtilBase.getKeyValue` 先读 `GameApp.p`，为空再读 `o`；后者非空时
  写回 `p` 并调用 `requestBackup`。
- `storeKeyValue` 分别比较 `o/p` 的旧值，仅在变化时写入；变化后请求 backup，返回 void。
- `c.a.j/n` 经 SharedPreferences 字段 `d` 和 Cipher 编码键/值；空或 null 走删除。
- 写入使用同步 `commit()`，其 boolean 返回被丢弃；解密异常分支调用 debuggerException。

私有 dexdump 定位行：`j:17547`、`n:17807`、`getKeyValue:34232`、`storeKeyValue:35458`。
不发布原始反汇编。两个 store 的后端、加密及 backup 都应纳入未来 native 契约，
不以简单字典或统一空值代替。

### 显式运行窗口中的证据

Frida QJS 环境没有 Java bridge；通过已有 JNI 表调用
`android.os.Debug.startMethodTracing/stopMethodTracing`，读取真实 ART v3 trace。
未安装新运行时或修改安全设置。trace 无 overflow，解析器检查布局、调用数和 root 栈。

**追踪只覆盖 cold-ready 后的显式 probe 窗口。四模式追踪在完整战斗和 reset 后启动，
不覆盖自然冷启动或自然整局的全部 Java 方法。** ART thread ID 与 Linux TID 未按数字强行对齐。

独立定位窗口 7 个 root、38,737 条方法记录、581,728 bytes，trace SHA256：
`7518898d6fb7ebe64821be3253a3e9aa412589a6679da739002734d10afbcf16`。
逐 root commit 数为 `[2, 0, 0, 2, 0, 2, 0]`；
commit、commitToMemory、enqueueDiskWrite、writeToFile 各观察 6 次。
这些调用处于对应原桥返回前的 Java root 中，root 正常返回。

四模式进一步观察：改变值、写空、恢复各触发两次 commit；相同值重复写不触发 commit；
读操作不触发 commit。四模式 trace 身份、记录数和逐 case 计数在
`e2_1b_evidence_summary.json`，原始参数不公开。

series-1 准备受控值后，series-2 的自然初始化实际读回该值并写回加一，
证明一次 fresh-process 转换。replacement 有意关闭桥 attach hook 以避免与 replaceFast
冲突，因此使用顶层 `PreferenceRead/Write` 观察真实值、顺序和线程；replacement
receipt 独立确认同线程 constructor scope 的两次 original_fallback。
不拼接两个 observer 的 sequence，也不把显式 probe 当成自然调用。

**仍未知**：commit boolean、写失败后的对外语义、断电 durability、backup 完成，
以及解密/配置异常触发与清理。ART 方法 trace 不记录返回值；同进程和新进程读回
不证明任意失败场景的持久化等价。PN 仍走原 JNI，没有 native 持久化实现。

## 4. 双线程限定实验

自有 ARM64 helper 创建两个 pthread，通过启动 barrier 同时放行；游戏调用外不加
串行 mutex。每线程最多四组“写相同保存值→读回”，均 completed=4、error=0、JNI detach 成功。
观察到 16 个 Java root（8 写＋8 读），6 对 root 的 wall 时间区间真实重叠，commit=0，
保存值前后相同。

- helper SHA256：`b0889af91320cfb5e12ff2ca80a71add99a66f52b4bd5d18959aa91ab2469a44`。
- trace：254,165 bytes、17,459 records，SHA256：
  `683b91284b412b34cbf2ef0778e514b6e810a94f3b29ae44012dde001f7c8158`。
- NDK 重编译使用 `-Wall -Wextra -Werror`，退出 0，复核二进制 SHA 与实测 helper 一致。

这仅验证同值、有界、正常路径的重叠读写，不证明不同值竞争、一般线程安全、配置对象
close/free 或异常清理。JNI pending exception 只检查、不统一清空；本次未触发异常。
helper 的自有字符串析构不等于 Java 引用退休或配置对象销毁。

第一次从临时目录加载 helper 被 SELinux 拒绝 mmap。保留失败，未改安全设置；
改为在独占克隆 app 的已有 native lib 目录新增自有 helper，未修改原游戏库。

## 5. 验证器、实际命令与结果

私有 manifest v2 绑定 21,861 个附件，SHA256：
`8c6b5ee1a7c87e362f157607365e8b6a637531ed6c91b4ffb108ce11a1221ded`。
验证器重新核对所有附件、五个不同 PID、输入/源码/配置身份、历史与本轮完整轨迹、
控制响应、资源门、32 键路由、PN 方法 trace、跨进程转换和并发结果。
三份实际运行的验证源码也须与冻结附件逐字节一致。

下列 `$PY` 是已有项目 Python，`$RUN` 是本轮私有独占根，`$OLD` 是只读旧 E2.1 证据根。
运行和输出均保存；复验必须选择不存在的输出文件，不覆盖历史证据。

```sh
"$PY" tools/run_e2_observed.py --config "$RUN/<run>.config.json" --output "$RUN/<run>"
# 上表五个 run 均 exit 0；配置分别绑定独占实例与模式。

"$PY" tools/verify_e2_1b_evidence.py \
  --manifest "$RUN/e2-1b-private-manifest-v2.json" --output "$RUN/bounded-evidence-2.json"
# exit 0：bounded_evidence_verified；strict_gate=BLOCKED

"$PY" tools/verify_e2_1_gate.py \
  --manifest "$OLD/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
# 实际 exit 2：旧严格门重验仍 BLOCKED，不宣称它已验收本轮新方法 trace。

E2_1B_PRIVATE_MANIFEST="$RUN/e2-1b-private-manifest-v2.json" \
E2_1B_ART_TRACE="$RUN/java-probe-1/java-methods.trace" \
"$PY" -m pytest tools/tests/test_e2_1_method_trace.py \
  tools/tests/test_e2_1_removability.py tools/tests/test_e2_1b_evidence.py \
  tools/tests/test_e2_1b_public_summary.py -q
# exit 0：65 passed in 7.99s，含真实附件及突变拒收。

"$PY" tools/e2_1b_public_summary.py \
  --manifest "$RUN/e2-1b-private-manifest-v2.json" --output-directory agent-handoff
# exit 0：重新验私有附件后，仅导出白名单字段；不复制配置值或原始审计。

"$PY" tools/run_main_engine_checks.py
# exit 0：211 passed in 41.29s

"$PY" tools/run_public_checks.py
# exit 0：主组794 passed/27 skipped；八专项见下。

node --check tools/e2_1_java_oracle.js
node --check tools/e2_1_concurrent_probe.js
# 均 exit 0
```

八专项依次为 23、82、15/3 skipped、25/4 skipped、31/3 skipped、31/2 skipped、
38/3 skipped、65 passed；集合重叠，不相加。公开入口不自动包含新增私有专项，
所以另列上述 65 项测试。最初未提供私有变量的 41 passed/7 skipped 只计工程检查。
脱敏器初次因带点键名拒收导致 1 failed/64 passed；修正键名校验并补回归，失败日志保留。
源码清单、凭据扫描、diff 和发布结果记于交付回执。

## 6. 失败留存与资源回收

`e2_1b_failure_index.json` 记录调试失败：零 methodID 文本格式、私有脚本遮蔽 Python
concurrent 包、SELinux helper 映射拒绝、账本脚本语法、跨 observer 归因假设、fetch HTTP2
以及脱敏器键名校验。保留首版 manifest 和原失败输出，不重写旧 E0/E1/E2 BLOCKED。
部分早期失败仅有工具输出，独立退出码未记录，索引明确为 null。

独占 engine、Frida、AVD、adb 和转发已回收，listeners remaining=0；核对原有 adb PID
及启动时间保持不变。只清理本轮已核验所有权的实例，保留克隆、输入和所有证据。
清理 receipt 实际 exit 0，未触碰冻结任务或原工作树。

## 7. 首个未解决依赖与下一步

第一阻塞是 **PN 原桥完整持久化/失败契约**：已有同步 commit 和双 store 的路径证据，
但 commit 的实际 boolean、写失败、不同值竞争及引用/配置对象释放尚未闭合。
46 键仍缺少消费者语义和可移除反事实；这两项不能用固定轨迹一致替代。

下一条最小实验继续限定 E2.1-B：在新的独占 oracle 中，先只读观察 PN 的两个
SharedPreferences store 身份及 commit boolean，再做保存/恢复完整的受控不同值交错，
分别记录原生桥返回、Java 调用和持久化结果；异常测试另建进程，未知类型显式保留。
随后对账本逐项追踪桥的消费者和返回值使用，从确有证据的候选键开始单键反事实，
保持原初始化顺序，禁止诊断 flag 跳过初始化。重复配置 constructor 的 ABI/所有权未明，
暂不在活对象上盲目重构造。E2.1 targeted strict、no-VM、Android-free 继续分开验收。
