# E0归因修正与E1资源提供器对照

## 结论及证据等级

基线提交 `74769af2c95fb095ae970a3b8c89934206805aea`，工作分支
`codex/headless-engine-plan-20261025`。本报告身份以所属提交为准。

**E0归因修正完成；E1局部原生对照通过，整体验收仍为BLOCKED。**
保留Java VM、原内容初始化顺序和配置JNI，只将libg的9个AAsset导入槽
重定向到独立只读APK提供器。没有改写原始libg文件。

- 已验证：最终provider版本的固定完整原生轨迹与原版基线3,602份快照全字节一致。
- 已验证：9,829个真实APK资源、849,482,445bytes与原AssetManager逐字节一致；
  read/seek/length/remaining/descriptor和两项缺失请求对照零差异。
- 运行时观察：替换provider共19,473次操作，失败0、未关闭asset 0；
  真实平台仍发生1次AAssetManager_fromJava，caller属于libfmod。
- 未覆盖：APK/update namespace优先级、引擎exists与回退分派的一般边界。
- 未知：FMOD获取平台函数指针的具体路径、两短stub的运行次数、未挂间接目标，
  以及移除VM后初始化与状态转移是否成立。

E1判定程序拒绝上述残留和缺证；不进入E2，不宣称无VM、无Android或10倍加速。
工程测试、固定轨迹语义和整体验收分别记录。

## 1. 隔离与身份

本机Darwin/arm64、macOS15.6、36GiB；复用已有Emulator37.1.11/API30 ARM64。
克隆独立e1-only，使用独立adb5041、控制及Frida端口。原adb5037及其他任务不动。
本实例guest仅允许loopback网络；未安装其他游戏版本或新系统组件。

| 输入 | SHA256 |
|---|---|
| libg.so | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 历史离线包装APK | `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b` |
| reference probe | `84cddf82e866540ff33d4b5f0c6b5b535c8485182e4cc84961028c7a56e65ec4` |
| 精确update资源manifest | `7a435be60fe6759509824194afa3748537c6cb4e945bdeaf7ae1bd3229cb7a82` |
| APK索引 | `6c9b427faeaba3a83c487754637be0eca7c4ab43516f660e2de813043babd1ed` |
| 最终Android provider构建 | `64a00fa317ff2449b107f8e626fea39b7ef8213f9b05d6f48587f068eb435e63` |

沿用旧固定match及28动作工程fixture，不增加训练seed。包装APK与supported_engine
原包SHA不同；只声明版本/ABI相符及所含libg字节精确匹配，不声明原APK相同。
最终再次核对APK/libg/probe及990个update文件SHA，全部不变。
输入、构建产物和原始审计仅在私有隔离目录；公开只保留通用自有代码与聚合。

## 2. E0阶段归因修正

observer新增每线程native scope栈、host_phase与线程清单，区分同线程嵌套和
并发时间重叠。私有入口配置去掉GameStateSetup的state_create强制阶段。
名称仍是候选名，不把名字当作语义证明。

| GameStateSetup阶段 | 实测次数 |
|---|---:|
| state_create_load | 1 |
| state_load | 9 |
| first_tick | 3 |
| sustained_early | 1,239 |
| sustained_middle | 2,400 |
| sustained_late_terminal | 1 |
| reset / terminal_reset_destroy | 各1 |

总计3,655，其中3,643位于Step scope，是热路径，不再归为纯创建阶段。
Step数=完整局3,601+两段21tick分支。

修正后实测JNI45,411、JavaVM11,911、AAsset19,474去重事件。
full_trace宿主时间窗内VM/JNI共19,079事件，主要来自并发Mainloop，
在已挂Step scope内未观测到这些事件。**这不证明tick无平台依赖**：
线程scope只覆盖已挂入口；部分scope是JNI辅助自身而非战斗调用链，平台异步
事件数受运行时长影响，不应要求与旧计数逐项相等。状态影响仍未知。

manager_b0.slot1与command.slot2仍挂钩失败，count=null、coverage=uncovered。
静态读取原文件表明两入口均是单条无条件分支且指向同一目标；邻接指令不应
算作该stub函数体。Frida尝试后局部运行字节存在差异，具体安装失败机制未确认。
未通过盲目扩展函数边界、替换stub或把目标别名次数当入口次数来补造覆盖。
queue/world/manager_218等旧间接覆盖缺口继续保留。

修正observer跑完整局、snapshot/restore及reset后，对原无插桩基线全字节一致；
这证明本固定轨迹上的观测不变性，不证明全部平台状态已序列化。

## 3. E1实现边界

- `tools/e1_apk_index.py`：核对ZIP central/local名称、压缩法、大小、范围及路径，
  支持stored/raw deflate，拒绝重复及非法条目；不提取或发布资源内容。
- `tools/e1_apk_provider.c`：只读pread、原始deflate、CRC核验、独立asset游标；
  stored返回真实APK fd、offset和length，compressed返回不可用描述符。
  缺失明确返回NULL/ENOENT；数据损坏计失败，不伪造空资源或ready。
- `tools/e1_resource_redirect.js`：模块加载时核验9槽原指针属于真实libandroid
  对应导出，才写入内存GOT并恢复权限；不替换JNI、配置或其他引擎依赖。
- `tools/e1_resource_compare.c`：独立诊断调用原版API和替换API，完整资源逐字节
  比较，追加EOF、seek失败/成功、remaining、长度、描述符及缺失检查。
- `tools/verify_e1_gate.py`：任何真实AAsset事件、资源hook缺口、provider错误、
  句柄残留或未验语义均阻止通过；不按caller过滤FMOD事件。

APK与update的原生分派代码保持原样，**保持代码不变不等于优先级已验证**。
本轮只证明APK API及固定轨迹；尚未构造同名冲突、仅update、仅APK、都缺失
等引擎入口对照，namespace_priority及fallback明确标为false。

## 4. 最终原生结果

最终replacement-v5：native terminal tick3601、28动作、队列0、3,602份快照，
9,120,440bytes全部逐字节一致，不归一化字段。轨迹SHA：
`0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5`。

snapshot0→step21→snapshotA→restore0→step21→snapshotB摘要及长度相同，
释放3handles；reset后完整局，终局再次reset。最终generation3/stateEpoch4/
tick0、finalized=false、snapshotHandles0、queuedCommands0。

最终provider：19,473操作、0失败、0 live assets、9,829索引项；9/9导入槽核验成功。
真实平台剩余1次fromJava，来源libfmod。独立libdl.dlsym观测未记录到AAsset解析，
这只排除了本次观测路径，**不是证明FMOD未动态解析**；其他resolver或既存指针未知。
静态FMOD符号/重定位中未找到AAsset项，仅属静态发现，不能据此解释实际指针来源。

在同一已完成轨迹进程中另行运行资源诊断：9,829资源、849,482,445bytes，
9,829描述符检查、2个缺失请求，差异0。此诊断故意调用原AssetManager，
与完整局计数分档，不能把诊断前的1次残留称为全生命周期零调用。
最终counts SHA：`4ac45c12143368045c9eb42c52878ab929d848e389f3c331e44091d80cb7d7d8`。
完整脱敏摘要及其他证据SHA见 `publication/e0-e1-resource-evidence.json`。

修正E0 trace耗时26.4285s、最终替换trace29.3472s，均有重插桩/快照/I/O开销；
不是无观测吞吐A/B，不据此声称提速或退化，也不换算10倍目标。

## 5. 实际命令与失败保留

下列变量为本地已有工具及私有目录。runner包含合法输入路径，未发布；通用实现
和独立合成测试可公开复验。两类证据不混用。

```sh
"$PY" "$RUN/run_corrected.py"
# exit0：E0修正、完整局、restore/reset
"$CLANG" -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared \
  tools/e1_apk_provider.c -lz -o "$RUN/libe1provider-final.so"
# exit0；Android API30 ARM64
"$PY" "$RUN/run_replacement.py" replacement-v5 replacement-v4
# exit0：最终源码完整原生对照运行
"$CLANG" -std=c11 -Wall -Wextra -Werror -O2 -fPIC -shared \
  tools/e1_resource_compare.c -L"$RUN" -le1provider -landroid \
  -Wl,-rpath,'$ORIGIN' -o "$RUN/libe1compare.so"
"$PY" "$RUN/compare_resources_v4.py" replacement-v5
# 两者exit0；全量真实资源逐字节对照
"$PY" tools/verify_e0_snapshots.py "$BASE/unobserved-snapshots" \
  "$RUN/replacement-v5/snapshots" --left-receipt "$BASE/unobserved-trace.json" \
  --right-receipt "$RUN/replacement-v5/trace.json" \
  --counts "$RUN/replacement-v5/dependency-counts-final.json" --output "$NEW_RECEIPT"
# exit0：3602份全字节一致，输出独占创建
"$PY" -m pytest -q -p no:cacheprovider tools/tests/test_e0_observer.py \
  tools/tests/test_e0_snapshot_verifier.py tools/tests/test_e1_resources.py \
  tools/tests/test_e1_gate.py tools/tests/test_rea_evidence.py \
  tools/tests/test_headless_native.py tools/tests/test_headless_trace_verifier.py
# exit0：111 passed in 6.25s；工程fixture不替代原生语义验收
node --check tools/e0_dependency_observer.js
node --check tools/e1_resource_redirect.js
# 均exit0
```

保留失败：实例启动一次adb远程参数错误；APK路径定位错误一次；
v1从临时目录dlopen被SELinux拒绝，未关闭保护；v2上一PID已退出导致断言失败；
v3完整轨迹通过但cache审计被既有初始化清理，pull失败exit1；
首次资源诊断缺少provider查找路径而dlopen失败，添加本实验$ORIGIN后通过。
v4与v5分别完成完整轨迹及资源对照，全部原始失败和成功输出留在私有隔离目录。

## 6. 工程回归与独立实例回收

`tools/run_main_engine_checks.py`退出0：211 passed in 53.14s。
`tools/run_public_checks.py`退出0：主组794 passed、27 skipped in 106.78s；
其余组分别为23 passed in 13.02s、82 passed in 218.82s、
15 passed/3 skipped in 0.31s、25 passed/4 skipped in 2.23s、
31 passed/3 skipped in 2.12s、31 passed/2 skipped in 2.65s、
38 passed/3 skipped in 5.46s、65 passed in 40.72s。
这些集合不累加；跳过不等于原生覆盖。

使用实际comparison、counts、redirect和semantics运行
`tools/verify_e1_gate.py`，退出2，明确输出BLOCKED及上述两个阻塞原因；
这不是运行器崩溃，也不是整体验收通过。输出独占创建，保留既有证据。

核验本轮AVD名、进程启动时间、game cmdline、Frida可执行路径及独立adb端口后，
仅终止本轮game与Frida，移除两项转发，正常退出AVD并关闭独立adb。
回收脚本退出0：所属监听残留0，原adb进程身份与启动时间完全不变。
克隆磁盘、完整成功与失败记录继续保留；未删除历史项目数据。

发布检查：`tools/check_public_snapshot.py`退出0，核验2,384项清单；
`gitleaks dir . --config .gitleaks.toml --redact`退出0，未发现泄漏；
`git diff --check`退出0。仅发布自有通用实现、工程测试与脱敏聚合，
原始审计、游戏资源、APK及构建二进制继续保留在私有隔离目录。

## 7. 未解决依赖与下一步

首个未解决边界是FMOD的真实AssetManager绑定及APK/update优先级与回退契约。
下一项应只观测FMOD指针写入/传递和真实资源分派，再在新隔离实例完成冲突、
缺失、fallback矩阵及完整轨迹复验。不得直接全局替换所有AAsset导出以隐藏计数，
也不得为了获得零值静默禁用FMOD或改配置JNI。

E1严格通过后，才提出E2无Java VM冷启动方案；本轮未实施或宣称E2。
训练门、main与冻结任务保持原样。
