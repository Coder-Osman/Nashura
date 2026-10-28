# E2.1-N：资源索引对象初始化链

基线 `9a7f5cd5352fd9f1b774cd540cc4d660b75af66d`。结果为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 动态观察

在匹配真实引擎与两端一致、前后稳定的 908 项 update 清单下，Android 同线程
观察到一个候选对象链：

1. 构造候选 `0x11b4df4` 中调用打开候选 `0x131a5f0`，两者的逻辑路径均为
   `assets.scdb`，打开候选的输出指针非空。
2. 构造返回时对象 `+0x70` 槽非空；同一对象随后进入初始化候选 `0x11b4d0c`，
   返回值为 1。
3. 前阶段的 14,653 次目标树路径处理均归属于同一初始化作用域；路径集合与
   匹配数据库的 `meta.path`、`asset.path` 分别逐项一致。

Linux 真实进程在 post-load 观察窗口内，对应构造、初始化和加载候选均全局调用
0 次。原生进程与 GDB exit 0，严格外层状态为 `BLOCKED`；构造器窗口未覆盖。
逻辑路径是相对名称，打开输出非空不独立证明实际物理路径、文件句柄或读取字节。
匹配引擎的静态指令使用命名 SQLite VFS；其注册及文件解析行为仍需运行时验证。
以上关联不证明整套资源语义、配置和 Backup 等价，也不证明插桩前后完整轨迹一致。

首次 Android 采样因观察脚本使用 Frida 只读属性而失败，记录 SHA
`12d9861a7f6c85d2123ffd8caf8f9c786110533dd8622ebb60869b478c3c780b`。
修正字段名后，预热采样的 update 清单发生变化；稳定清单后的新采样脚本错误、
丢事件和活动作用域均为 0。专用 AVD 已关闭，私有路径、数据库、日志和二进制
未发布。

## 命令与验证

```sh
# Linux 使用前述隔离 provider_job 参数及匹配输入，改用
# --observe-targets tools/pure_linux/database_init_targets.json。
# native/GDB exit 0，严格状态 BLOCKED。
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0，update 清单前后相同
python tools/pure_linux/audit_database_init.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" \
  --database "$PRIVATE_DB" --database-sha256 "$DB_SHA" --output "$AUDIT"
# exit 0；数据库只读、前后 SHA 一致
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1062 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
数据库 SHA `4b99bfd5d5fb52fe79bba74cd1990ce08fc4f46064d905c64cb5c281b6474e7b`；
审计 SHA `786eeabc42e8cceac00a385899efc973a58934dd531f75d5414269a777b9035e`。
更多身份见[机器证据](pure-linux-database-init-evidence-20261029.json)。
下一步确认构造入口的原始调用参数、SQLite VFS 的文件解析与所有权，再在隔离
Linux 子进程试验真实初始化；严格门通过前 ResourceManager、GameState 和 Step
保持 NOT_RUN。
