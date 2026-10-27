# E2.1-C：PN_POPUP 真实提交返回、写失败与不同值交错

## 阶段结论

本轮推进 E2.1-B 的未闭合项，取得原 Java 存储后端的真实 `commit()` 返回证据，
以及受控写失败和不同值交错的原生观测。**限定证据验收通过，G-E2.1 仍为 BLOCKED。**
PN_POPUP 仍经原 JNI、原加密和原双 store；未增加 native replacement，未移除排除键。
46 个排除键的保守 required 路由保持；战斗必需性和可移除性继续为 unknown。

工作基线为 `68d015c68068e7d2d67d0b80b1d2b1a88d4c3e9a`，分支为
`codex/headless-engine-plan-20261025`。本次交付 SHA 为包含本报告的阶段提交，
最终回执消息提供本地和远端实际 SHA。main/origin/main 保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。历史 BLOCKED 和失败附件全部保留。

## 方法和观测边界

`E21PreferenceObserver.java` 暂时将两个原 SharedPreferences 引用包装成转发代理。
代理保留原对象调用、Editor 链式返回和原异常传播，记录原对象实际返回的 boolean；
并非根据 ART 方法名、原生返回寄存器或磁盘变化推测提交结果。日志不记录配置值。
原桥写 ABI 为 void，始终标为 `void; raw register not interpreted as status`。

`run_e2_observed.py` 的新增可选配置只在 **cold-ready 之后、GameState 创建之前**安装代理，
finally 保存事件并恢复两个原引用；观测清理失败会令该次运行失败。旧入口默认行为保持。
这不覆盖自然冷启动期间的 commit 返回，也不替代 JavaVM、配置 JNI 或存储后端。
代理记录使用 Java thread ID；native probe 使用 Linux TID，ART 使用 trace thread ID，
三者不按数值相同进行线程关联。

写失败仅在本轮独占克隆中，暂时将 shared_prefs 目录设为 0555、两个 XML 设为 0440，
并保存权限、字节摘要和真实 logcat。实验后恢复原权限和保存的 PN 值，再核对双 store。
原 APK、libg、libfmod、probe、Provider 和 990 个 update 文件身份保持。

不同值实验由两个自有 pthread 各执行四轮 barrier 对齐的原桥写/读；使用原字符串
构造/析构、原 JNI attach，检查 pending exception，不清除异常，完成后 detach。
受控值仅用于独立实验，未写入公开摘要。未并发执行其他配置替换。

## 完整原生轨迹与非扰动验证

六个独立原生进程：baseline、observed、diagnostic、observed-v2、diagnostic-2、diagnostic-3。
observed-v2 使用本次发布的 Java observer 源码；早期 observed 的源码档案单独保留。

| 项目 | 每个进程的实际结果 |
|---|---|
| runner 退出码 | 0 |
| 原生快照 | 3,602 个、9,120,440 bytes，逐字节一致 |
| 动作及终局 | 28 个固定动作、tick 3601 原生终局 |
| 生命周期 | snapshot / restore / reset 控制响应一致 |
| 轨迹 SHA256 | `0854eaeda7747e27b1d860268a76f87cc184ad1c5d14e97d4d027e49155441f5` |
| 平台 AssetManager | 实际事件 0 |
| Provider | failures=0，live_assets=0 |

未归一化任何快照字节。六局只覆盖既定轨迹，不代表任意动作、配置故障期间的战斗或任意调度等价。
显式故障与竞争探针在完整局之后单独执行；不把它们拼成正常战斗生命周期。

## 实际 PN 结果

### 顺序调用

| 原桥操作 | 原 store o | 原 store p |
|---|---|---|
| 变化写 | commit(true) ×1 | commit(true) ×1 |
| 随后读 | 无 commit | 无 commit |
| 同值写 | 无 commit | 无 commit |
| 空值写 | commit(true) ×1 | commit(true) ×1 |
| 空值读 | 无 commit | 无 commit |
| 恢复保存值 | commit(true) ×1 | commit(true) ×1 |
| 恢复后读 | 无 commit | 无 commit |

以上是 post-cold-ready 显式窗口的实际返回，不外推自然冷启动和其他键。

### 原后端写失败

- 变化写的两个原对象均实际返回 `commit(false)`，各一次。
- 原桥返回类型仍为 void；probe 线程 JNI ExceptionCheck 前后均为 false。
- 原桥以及双 store 的同进程读取均得到新值；故障窗口两个 XML 字节摘要保持原样。
- logcat 分别记录 storage.xml 和 storage_new.xml 重命名至备份失败，各一次。
- 恢复权限后用不同值写入，双 store 均返回 true；最后恢复保存值并核验双 store 和权限。

**关键契约：本次磁盘提交失败没有回滚内存值，也没有经该 void 桥传播类型化错误。**
将 native 替代实现简单写成“失败即保持旧内存值”会与此观测冲突；同进程读到新值也不是持久化成功。
尚未验证进程崩溃、断电恢复、异步重试或所有 I/O 错误。

### 不同值并发

两个线程共 8 写、8 读；各四轮完成、error=0、detached=true、unknown_reads=0。
实际 7 次读到本线程值、1 次读到另一线程值，ART root 时间窗有 6 对真实重叠。
双 store 的实际 commit 为 o:true ×7、p:true ×6，ART 选定 commit 调用总数与转发记录一致。

原实现的逐 store 同值判断和交错使提交次数不必相同。该次观测不证明线性化、双 store 原子性、
一致调度重现或一般线程安全；另一线程值可见不被伪装成实验失败，也不被当作语义全部闭合。

## ART 覆盖缺口和证据验证

写失败 trace 中有 6 个未命名 method ID / 28 条记录，竞争 trace 中有 6 个 / 200 条。
其来源仍 unknown；动态 Proxy 仅是待验证假设。
原 `e2_1_method_trace.py` 保持严格拒收未知 method ID，未放宽旧门。

新 `e2_1_partial_method_trace.py` 是独立限定解析器：只认方法表中已命名的真实 bridge root，
未命名 ID 保留为 opaque 整数、原始栈身份和计数，检查 root 栈闭合及 exit/entry 对应关系。
输出 `exhaustive_method_coverage=false`。它不为未知 ID 补造名字，不声称完整 Java 覆盖；
ART 本身没有返回值，boolean 来自独立转发观测。

`verify_e2_1c_evidence.py` 验证 22,039 个 SHA 绑定的私有附件、路径边界、六独立进程、
完整轨迹、资源计数、输入身份、源码档案、helper 上传与复编字节、权限/保存值恢复及保留失败。
结果仅为 `bounded_evidence_verified`，严格门和 no-VM/Android-free/训练/10×字段保持未通过。
白名单导出见 `e2_1c_evidence_summary.json`；公开摘要不含原始 trace、配置值和私有路径，
也不足以独立重验原生结论。重验需要本地匹配输入和私有附件。

## 构建、实际命令与测试

以下 `$PY` 指本轮使用的项目 Python，`$RUN` 指独占私有证据目录，`$OLD_RUN` 指旧 E2.1
证据目录；它们不是公开仓库自带附件。每条输出均使用新文件名，避免覆盖失败记录。

```sh
# 六个完整对照均实际调用此入口，各自使用独立配置及输出目录；每次 exit 0。
"$PY" tools/run_e2_observed.py --config "$RUN/baseline.config.json" --output "$RUN/baseline"
"$PY" tools/run_e2_observed.py --config "$RUN/observed-v2.config.json" --output "$RUN/observed-v2"

"$PY" tools/verify_e2_1c_evidence.py --manifest "$RUN/e2-1c-private-manifest-v2.json" \
  --output "$RUN/bounded-verification-2.json"
"$PY" tools/e2_1c_public_summary.py --manifest "$RUN/e2-1c-private-manifest-v2.json" \
  --output agent-handoff/e2_1c_evidence_summary.json
E2_1C_PRIVATE_MANIFEST="$RUN/e2-1c-private-manifest-v2.json" "$PY" -m pytest -q -p no:cacheprovider \
  tools/tests/test_e2_1_commit_events.py tools/tests/test_e2_1c_public_summary.py
"$PY" tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" \
  --output "$RUN/strict-gate-recheck-1.json"
node --check tools/e2_1_return_observer.js
node --check tools/e2_1_race_probe.js
"$PY" tools/run_main_engine_checks.py
"$PY" tools/run_public_checks.py
```

限定验证与白名单导出 exit0；真实附件专项 **50 passed / 14.39s，exit0**，包含负向突变，
测试 fixture 只验证工程边界。旧严格门实际重验 **exit2 / BLOCKED**，不以限定验收替代。
两 JS 语法检查 exit0。主工程 **211 passed / 40.30s，exit0**。
公开主组 **794 passed / 27 skipped / 82.19s**；八个后续专项结果在阶段回执记录。
测试集合重叠，不相加；工程测试不代表无 VM 或原生语义完整闭合。

Java observer 与 helper 按当前公开源码复编，使用 Android API30：

```sh
"$JDK/bin/javac" --release 8 -classpath "$ANDROID35_JAR" -d "$BUILD/classes" tools/E21PreferenceObserver.java
"$D8" --lib "$ANDROID35_JAR" --min-api 30 --output "$BUILD/dex" <按序排列的 class 文件>
"$NDK/bin/aarch64-linux-android30-clang" -shared -fPIC -O2 -Wall -Wextra -Werror \
  tools/e2_1_race_probe.c -o "$BUILD/libe21race.so"
```

三条成功构建命令 exit0；DEX 和 SO 与实测字节分别完全一致：
`de22ba8980c8ab9e8e617bc7f3fdcd4a60339c9a36c9ea8dac42ec7e59a1ccdb`、
`34afb9353d7958df9449599ab49e1a9d2c9bb479867f603e702fe5dc3c7b1c0b`。
自有 DEX/SO 也只留本地，不随公开仓库上传。

第一次构建因错误 JDK library 输入失败，第二次 API24 构建虽然编译成功但字节不符；
第三次使用 API30 恢复一致。早期 probe 脚本和 race 附件归档错误保留，最终第三轮才验收。
详细脱敏索引见 `e2_1c_failure_index.json`，不重写旧失败为成功。

## 资源回收与下一项最小实验

本轮独占原生进程、Frida、AVD 和专用 adb 已按 PID/启动身份核验后回收；
5056、5674、5675、27079、29018 监听均为0，原有 5037 adb 的 PID/启动时间/命令保持原样。
克隆磁盘及全部失败附件保留。没有接管其他运行实例、修改系统安全设置或安装新系统组件。

首个尚未闭合依赖是 PN 的原生替代持久化/失败契约及 Java 引用生命周期，
而不是缺少更多固定轨迹样本。下一轮限定工作应分别完成：

1. 自然冷启动 commit 返回的非扰动观测，验证初始化时机，保持原内容初始化顺序。
2. PN 原生候选在正常、同值、空值、失败后内存可见及持久化边界逐项对照，先不接入整体替换。
3. 为46键建立实际消费者和可移除反事实；失败逐项保留，未知继续原路由。
4. 配置 close/free、Java global reference 退休、重复初始化对象 ABI 独立验收。

以上项目仍属于 E2.1；本次未进入 E2.2/no-VM、Android-free、训练、G0/G1 或10×实验。
