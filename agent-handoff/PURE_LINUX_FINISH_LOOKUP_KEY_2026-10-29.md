# E2.1-N：完成函数查询键的双端对照

基线 `3c1dd7ffb890ecab7de284d62ca893f9a782ca34`。以相同908项 update 清单和
匹配版本的真实 `libg.so`，在独立 Android 与 ARM64 Linux/Bionic 进程中观察
`finish_a` 内的候选查询；结果为 **RUNTIME_OBSERVED_LIMITED**，严格 catalog 门
仍为 false。

## 运行结果

- 两侧查询各调用一次，参数按已有 `engine-string16` 契约读取：各为30字节，
  长度与终止符检查通过，字节逐项一致；脱敏 SHA 为
  `b225b5fac2af40343261e33611854570a0f109ce2458c8beeff21d06b4be66f1`。
- Android 查询返回非空，Linux 返回空；两侧调用前的 `context+0xb60` 槽均非空，
  已观察17槽行数同为1,950。调用返回后分别为3,367和1,950行。
- Android 前后 update 清单相同且无脚本异常、丢事件和活动作用域；Linux 引擎
  输入及资源前后不变、原生/GDB退出0，运行器状态 BLOCKED。两侧仍为不同进程
  和运行时；相同键不证明查询容器内容、对象初始化或未观察的并发行为相同。

Linux 首次以 libc++ 布局读取该参数被边界检查拒收，未把失败当作空字符串；
另一次新进程使用已有 `engine-string16` 契约成功读取。失败样本 observation SHA
`6404d31872f04e814fef01c36f130c00555e16115c6a5e6e76b90a2e61f0444d`。Android
使用相同的长度、边界和终止符语义；查询键原文及私有内存不发布。

## 实际命令

```sh
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$MATCHED_UPDATE" --data-path / \
  --search-root "$MATCHED_UPDATE" --factory-mode register --resource-manifest "$MATCHED_MANIFEST" \
  --asset-root "$DATA" --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 \
  --catalog-mode loader --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$KEY_TARGETS" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper nonzero; native/GDB exit 0; status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0; bounded capture
python tools/pure_linux/audit_finish_lookup_key.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0; bounded key comparison
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1028 passed, 1 skipped
```

Android结果 SHA `f7cc97ac2337d52bfab64376feabab1ef3ef3aa33a118ab2388156502929bef5`；
Linux结果 SHA `9d604ac0c5114957b88c725ab6ddb923fc455e6ca982f30aec813efd0a088385`；
GDB观察 SHA `977293563ebc96960fa3845763a01c217801765f988aec1eb6bc1e6ba910d80c`；
审计 SHA `519c77b19ce07e04c4437c7d47bfacf569e0058ddd112459bbf7db34daa06066`。
真实引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
首次错误布局解析的失败证据、原始日志与资源保留在私有目录。专用 AVD 已关闭。

下一步跟踪匹配版本中查询对象的构造、数据填充和查找分支，验证相同键为何在
Linux 返回空。严格 catalog、配置异常和 Backup 等价未通过，战斗生命周期仍 NOT_RUN。

[机器证据](pure-linux-finish-lookup-key-evidence-20261029.json)。
