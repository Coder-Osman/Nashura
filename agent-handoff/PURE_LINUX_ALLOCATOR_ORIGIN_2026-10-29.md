# E2.1-N：持有者分配器构造来源

基线 `301a2ea`，状态 `RUNTIME_OBSERVED_LIMITED`；严格
`CATALOG_READY_PASS=false`。

匹配库的动态重定位将分配器虚表 `0x18bf798` 指向入口 `0x1237358`；
只读指令显示候选构造器 `0x1237348` 设置该虚表并保留第二参数。上游指令在
对象 `+0xf0` 调用构造器，并传入库内字符串 `AndroidMainAllocator`。

隔离 Android 自然启动中，已观察到返回地址 `0x139dd04` 调用候选构造器，
第二参数实际为 `AndroidMainAllocator`，对象字段与参数相同且虚表匹配。
后续持有者工厂使用同一地址的分配器；构造和工厂运行在不同线程。该地址关联
未覆盖完整析构和重用过程。工厂第二参数非空并被保存到持有者字段，其类型、
生命周期和 Linux 等价对象仍为 `UNKNOWN`。

首次预热时 update 清单由 990 变为 956 项，稳定复测前后均为 908 项。v4 因
追踪代码把参数绑定到错误的回调作用域而记录脚本错误，保留其失败结果；修正后
v5 无脚本错误和丢事件，身份审计通过。独立只读 AVD 已关闭；未修改原始库。
未使用未知第二参数调用 Linux 工厂，未覆盖配置异常或 Backup 等价。

## 验证

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 最终稳定运行 exit 0，update 前后各 908 项
python tools/pure_linux/audit_allocator_origin.py --result "$ANDROID_OUT/result.json" \
  --result-sha256 "$RESULT_SHA" --script tools/pure_linux/android_catalog_start_trace.js \
  --output "$PRIVATE_AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1085 passed，1 skipped
```

完整身份见[机器证据](pure-linux-allocator-origin-evidence-20261029.json)。私有日志
位于 `/private/tmp/nashura-allocator-android-20261029-v1` 至 `v5`，未发布。
下一步追踪工厂第二参数的构造来源及分配器释放，再在 Linux 隔离进程验证
真实工厂调用。严格 catalog、ResourceManager、GameState、Step、完整对局和
性能评估均未通过或未运行。
