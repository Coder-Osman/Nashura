# E2.1-N：目标树路径与 SQLite 索引对照

基线 `df0b2c0b262b04fca2ab0c70d127d3fd6dab4932`。本阶段为
`RUNTIME_OBSERVED_LIMITED`；严格 `CATALOG_READY_PASS=false`。

## 证据

Android 使用匹配版本的真实引擎，update 清单前后均为 908 项且与 Linux 逐项一致。
在目标树查询之前，上游入口 `0x11b0820` 针对同一对象调用并返回 14,653 次，
每次路径不同，调用返回位置均为 `0x11b4534`；全部发生于同线程候选加载函数
`0x11b4248` 的作用域内，其调用返回位置为 `0x11b4d40`。
Linux 在 post-load 观察窗口内，上游入口全局调用 0 次，原生/GDB 退出 0，
外层运行器仍为 `BLOCKED`。Linux 构造器未在该观察窗口内。

从同一 Android update 清单只读复制 `assets.scdb`，其 SHA 与 Linux 清单相同；
系统识别为 SQLite 3 数据库。只读、immutable 查询显示，Android 所观察的
14,653 个完整路径与其 `meta.path`、`asset.path` 两个集合分别逐项相同。
匹配引擎的静态指令在候选构造函数中引用了读取 `meta` 列的 SQL 字符串；这是
静态发现。对象关联、同线程作用域和集合一致支持追踪该数据库加载链，但还未
观察数据库句柄及读取字节的直接来源，不能单独认定具体调用因果或等价完成。

Android 首次预热的 update 清单从 990 项变为 952 项，作为预热记录保留；后续
采样均为 908 项且钩子错误、丢事件和活动作用域为 0。路径和数据库内容、原始
二进制及私有日志留在本地隔离目录，不进入公开仓库。插桩前后的完整轨迹一致性
不在此次覆盖范围；配置异常与 Backup 等价仍未闭合。

## 验证

```sh
# Linux 使用前一阶段的隔离 provider_job 参数和匹配输入，改用
# --observe-targets tools/pure_linux/path_source_targets.json；
# native/GDB exit 0，严格状态 BLOCKED。
adb -s emulator-5630 pull "$UPDATE/assets.scdb" "$PRIVATE_DB"
# exit 0；数据库 SHA 与 Linux 资源清单相同
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0；908 项 update 清单前后相同
python tools/pure_linux/audit_path_source.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" \
  --database "$PRIVATE_DB" --database-sha256 "$DB_SHA" --output "$AUDIT"
# exit 0，SQLite 只读打开并校验前后 SHA
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1056 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
数据库 SHA `4b99bfd5d5fb52fe79bba74cd1990ce08fc4f46064d905c64cb5c281b6474e7b`；
审计 SHA `3c5d2df9a07d1a49f78629fa60ebec373b92e06b3f6301bd00bca23c39851406`。
其余输入及结果身份见[机器证据](pure-linux-path-source-evidence-20261029.json)。
专用 AVD 已关闭。下一步验证数据库对象构造、打开路径与真实加载调用，明确 Linux
缺失的前置操作；ResourceManager、GameState、Step、完整对局及性能验收未运行。
