# E2.1-N：数据库工厂与真实打开路径

基线 `0c649b78f2457d708673387f8fce17658810f5d6`；结果为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 真实观察

在匹配真实引擎、两端逐项一致且 Android 前后不变的 908 项 update 清单下：

- Android 同线程候选初始化入口 `0x75760c` 由返回位置 `0x756e94` 调用。
  其所属对象与既有 content root、context 均不同；`+0x3f0` 槽进入时为空，返回
  后非空。对象身份和所有权目前未知。
- 该作用域进入数据库工厂 `0x11b4b30`，返回位置 `0x757648`，工厂输出非空；
  对象构造、逻辑 `assets.scdb` 打开和先前记录的数据库初始化在同一链中。
- 构造作用域内，两次真实 libc `open` 均成功，路径均为游戏私有 update 目录
  的 `assets.scdb`；两次上层文件系统打开均非空，虚表偏移为 `0x18c3130`。
  同一文件的只读副本 SHA 与 Linux update 清单一致，且其路径集合与 Android
  观察的 14,653 项逐项一致。

这些证据验证了特定运行中打开请求的路径与调用作用域；尚未逐字节追踪所有读取、
文件描述符复用或证明整局轨迹不受插桩影响。Linux 对应三个数据库候选入口在
先前 post-load 观察窗口内为零，构造器窗口未覆盖。静态指令显示此初始化与已有
packed-reader 注册位于同一上游函数，但 Linux 缺失对象的创建条件仍需动态验证。
配置异常和 Backup 等价仍未闭合，不能由此放行严格 catalog 门。

首次 Android 采样为只读 AVD 预热，update 清单从 990 项变为 936 项；最终采样
前后均为 908 项，钩子错误、丢事件和活动作用域均为 0。原始路径、数据库、日志
和二进制保留在私有隔离目录，不进入公开仓库。专用 AVD 已关闭。

## 验证

```sh
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0，908 项 update 清单前后不变
python tools/pure_linux/audit_database_factory.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" \
  --database "$PRIVATE_DB" --database-sha256 "$DB_SHA" --output "$AUDIT"
# exit 0；验证身份、父子作用域、成功打开和数据库路径集合
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1068 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
数据库 SHA `4b99bfd5d5fb52fe79bba74cd1990ce08fc4f46064d905c64cb5c281b6474e7b`；
审计 SHA `a69cd58d67a908b4b669a2aaeb6539a14e76c45f8c9345653977f68efa90fe23`。
其他身份见[机器证据](pure-linux-database-factory-evidence-20261029.json)。
下一步追踪初始化对象的构造及生命周期，复现真实调用参数后在隔离 Linux 子进程
尝试数据库初始化；ResourceManager、GameState、Step 和完整对局仍未运行。
