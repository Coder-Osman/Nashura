# E2.1-N：查询树填充候选的双端观察

基线 `7fbd7b8cc8c613428ddb0ce2b2b1c888c5c21f4f`。此阶段仅为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 结果

以真实匹配 ARM64 libg、两端逐项一致的 908 项 update 清单和同一个查询键，
观察此前定位的查询树。Android 在目标树查询前，按对象身份分别观察到：

| 候选入口 | 调用/返回 | 调用返回位置 |
|---|---:|---|
| `0x11b0084` 节点创建候选 | 8 / 8 | `0x11aff30` |
| `0x11aebb0` 填充候选 | 8 / 8 | `0x11b02bc` |
| `0x11b2250` 树插入候选 | 8 / 8 | `0x11aecc8` |

Linux 独立原生进程在 post-load 标记之后的完整观察窗口内，以上三个入口的
全局调用次数均为 0；目标树根为空。Android 目标树根非空、相邻机器字为 8，
首段查询命中。这些匹配同一对象的计数支持继续追踪节点路径，但不单独证明
每次调用之间的强因果关系，也不排除其他填充路径。Linux 构造器未覆盖，不能
把此窗口的零计数泛化为整个进程生命周期或所有输入。

静态机器指令表明 `0x11afc60` 根据路径分段查询并可能调用 `0x11b0084`；
其直接调用来源及输入来源需另行动态核验，不将函数名或业务语义写成已验证。
Android 首次预热清单从 990 变成 956 项，该次仅作预热；后续 v4 的前后清单
均为 908 项且 SHA 一致。早期候选 hook 最初在第一批表开始时才安装，已移至
模块观察阶段；前一结果的零计数不覆盖安装前阶段。

Android 受控进程和 Linux 原生/GDB 均 exit 0；Linux 严格运行器仍为 `BLOCKED`。
Android 脚本错误、丢事件及未关闭作用域均为 0。完整轨迹不在本次观测范围。
配置异常、Backup 等价、完整 catalog 资源和初始化语义仍待验证。

## 复现与验证

```sh
# 与前一阶段相同的独立 Android 启动和已核验 Linux provider_job 参数，
# Linux 改用 --observe-targets tools/pure_linux/tree_population_targets.json。
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# Android exit 0; 908 update files unchanged
python tools/pure_linux/audit_tree_population.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1048 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

匹配引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
审计 SHA `4dabfcb6a9dc24132fb93aa050a4424b0d816ea4c6aa3179ff3cbe598674492e`。
其他 SHA 和限定状态见[机器证据](pure-linux-tree-population-evidence-20261029.json)。
专用 AVD 已关闭，私有日志与二进制留在隔离环境。

下一步观察 `0x11afc60` 的上游调用与路径输入，核对 Linux 缺失的调用条件；
未通过严格 catalog 门前，ResourceManager、GameState、Step、完整对局和性能
验收均保持 NOT_RUN。
