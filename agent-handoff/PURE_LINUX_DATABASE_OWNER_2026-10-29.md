# E2.1-N：数据库初始化所属对象与时间边界

基线 `cf3da20c41b760e3b985535efcf0ffd04e23836c`。本阶段为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 修正与新观察

上一阶段将初始化时的对象地址与 content root/context 比较，记录两者均不相等。
本次确认在该时点 **root 和 context 均为空**，因此当时的比较只能说明目标对象
非空，不足以支持它与已构造的 root/context 不同；历史报告保留，此处明确修正。
该时点针对 root/context 的引用扫描标记 `NOT_COVERED`。

同一 Android 独立进程后续到达第五批 catalog `finish_a` 前，root 和 context
均非空，保存的初始化对象地址与两者地址均不相等。该比较未追踪目标对象的分配、
释放和地址复用，因此不证明整个生命周期内的身份或所有权。初始化时对真实库
可写映射做有界扫描，没有发现该对象指针的匹配槽；扫描不覆盖 heap、栈、其他库
或标签位不同的指针，不能据此排除其他持有者。

对象首指针对应匹配 libg 的 `0x17ea568`。只读 ELF 动态重定位显示偏移
`0x17ea580` 指向 `0x756be8`，该函数静态包含上一阶段观察的
`0x756e90 → 0x75760c` 调用。这是版本绑定的静态结构证据；对象构造入口、
具体类型和虚调用来源仍需动态验证。

双端资源和运行身份通过既有数据库工厂审计：Android 的 update 清单前后 908 项、
脚本错误及丢事件为 0，真实文件打开与数据库路径集合核验通过；Linux 数据库
候选在 post-load 观察窗口内为零，构造器未覆盖。初次 Android 预热清单从
990 项变为 941 项，仅作为预热记录保留。专用 AVD 已关闭。

## 验证

```sh
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0；908 项 update 清单前后相同
llvm-readelf --dyn-relocations "$LIBG"
# exit 0；仅检查匹配版本的目标重定位，原始输出留在私有目录
python tools/pure_linux/audit_database_owner.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" \
  --database "$PRIVATE_DB" --database-sha256 "$DB_SHA" --output "$AUDIT"
# exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1073 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
审计 SHA `a4bd40c590921d49d71cb18e9a64fc7f9bb667a55aa702c523bc51f3d633f7ac`。
其余身份见[机器证据](pure-linux-database-owner-evidence-20261029.json)。
下一步追踪该虚表实例的真实分配、构造及启动入口，核实 Linux 所需的对象所有权；
配置、Backup、catalog、战斗生命周期和性能门仍未完成。
