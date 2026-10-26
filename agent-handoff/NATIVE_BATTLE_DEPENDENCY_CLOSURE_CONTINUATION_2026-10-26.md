# 原版战斗依赖闭包：边界时间盒及E0续报

## 身份

基于 `b3947d8720fa0645f78acf15f72546992da2d4b6`，继续现有 `codex/headless-engine-plan-20261025` 工作树。
不改main、原始libg、冻结部署和训练门；本轮代码SHA以报告所属提交为准。

## 1. 两个反编译边界问题

使用独立Ghidra12.1.4数据库和匹配ARM64 libg，依次比较显式范围、预声明41个callee、显式数据库尾流覆盖。

| 候选函数 | 原越界P-code | callee声明后 | CALL_RETURN覆盖后 |
|---|---:|---:|---:|
| NativeReplayRunner | 9 / 2地址 | 4 / 1地址 | 0 |
| ReaderRegister | 2,308 / 192地址 | 2,023 / 192地址 | 0 |

汇编219/175条及直接BL32/28条三组不变，源SHA不变。
只在隔离数据库限制分析控制流，不写ELF。越界归零不证明尾调用/无返回假设、PLT/PIC、实际ABI或函数语义正确。
未经覆盖的原失败导出保留；按时间盒结束，不无限阻塞动态实验。

```sh
"$GHIDRA_INSTALL_DIR/support/analyzeHeadless" "$ISOLATED_PROJECT_DIR" bounded-review \
  -import "$MATCHING_LIBG" -noanalysis -scriptPath tools/ghidra \
  -postScript ExportBoundedFunctions.java \
  110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783 \
  "$ROOTS_TSV" "$EXPORT_DIR" "$CALLEES_TSV" bound-terminal-calls
# exit0，三组各用独立数据库/输出
"$PY" -m pytest -q -p no:cacheprovider tools/tests/test_bounded_ghidra.py
# 显式设置本地官方GHIDRA_INSTALL_DIR，4 passed，18.36s，exit0
```

新增自构ARM64 ELF真实Ghidra测试：越界差分、错误指纹、范围重叠、非文件支持地址；不依赖公开游戏样本。

## 2. 平台纠正和E0资格

Darwin/ARM64不执行旧包Linux ELF/x86-64 QEMU，其Exec format error不是引擎失败。
Android模拟器路线无需另建Linux，但仍包含Android内核、Bionic、VM和框架。
初次ADB设备为空曾标BLOCKED_BASELINE；用户授权安装实例后，克隆历史AVD完成资格核验，现已解除该阻塞。

离线包装APK与supported_engine原包SHA不同，但包版本、ABI和提取libg指纹核验一致。
精确990资源前后SHA相同，只在本轮克隆内使用历史reference probe，不改原始libg和历史部署。

## 3. E0实际结果及限制

无插桩与两次成功插桩均原生完整终局，tick3601、28动作、队列空。
每次3602份原生serialized快照全字节等同基线；restore分支、句柄释放和reset通过。
真实捕获JNI/JavaVM/AssetManager、配置桥和命令间接调用；两短stub挂钩失败及对象表/线程阶段归因缺口尚存。
状态影响未知，不宣称覆盖穷尽或无VM。完整计数、命令、身份及失败记录见
[E0动态报告](E0_DYNAMIC_DEPENDENCY_COUNTS_2026-10-26.md)。

## 4. 后续顺序

E1/E2未执行；下一步独立E1保留VM，只替换资源provider，要求完整局/snapshot/reset对照且实测AssetManager为零。
E2之后依据实测配置建立有界契约，未知键/写入/资源请求显式失败，保持内容初始化顺序。
无VM冷启动完整局和10倍吞吐仍未验证，训练准入不变。
