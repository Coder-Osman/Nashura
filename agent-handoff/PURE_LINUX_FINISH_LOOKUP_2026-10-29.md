# E2.1-N：catalog 完成函数内候选查询分歧

基线 `30a803acdd8c4c11a89654cec8964ecf45a5be22`。以相同908项 update 清单、
匹配真实 `libg.so` 的独立 Android 和 ARM64 Linux 进程完成受限动态对照；
状态 **RUNTIME_OBSERVED_LIMITED**，严格 `CATALOG_READY_PASS=false`。

## 实际发现

- 两侧 `finish_a` 入口对应 context 的 `+0xb60` 指针槽均非空，17槽行数同为1,950。
  因此当前样本中 Linux 并非因该槽为空而提前返回。指针值和对象内容未比较。
- 在 `finish_a` 作用域内，版本绑定候选查询 `0x1207ffc` 双方各调用并返回一次；
  Android 返回非空，Linux 返回空。该调用后的 `finish_a` 结束边界，Android
  17槽为3,367行、Linux为1,950行。该时间关联不单独证明唯一因果，查询键、
  指向对象的内容和初始化来源仍未知。
- Android 受控启动后采集前后 update 清单均908项，脚本异常、事件丢失及活动
  作用域均为0；Linux原生/GDB退出0、输入和资源身份不变，运行器状态 BLOCKED。
  两者是不同进程和运行时，采集只覆盖显式入口及17槽。

## 静态分析与失败记录

复制此前 Ghidra 分析库到私有目录，使用 `-readOnly -noanalysis`，在副本的内存
事务中创建目标函数并反编译；程序的 executable SHA 与真实输入一致，目标入口
及推断边界已记录，反编译完成。输出表明存在空槽提前返回及候选查询的空返回
分支；这是静态发现，参数、业务语义与全部分支需要运行时进一步验证。

首次仅查询既有入口时，Ghidra进程退出0但脚本报告 `function entry missing`，没有
生成目标输出；保留日志。第二次创建临时入口后反编译完成并丢弃项目更改。
此前完整 REA 全量导入分析耗时过长，人工中止并保留失败；不把取消报告为引擎或
函数故障。原始伪代码、地址日志、游戏文件及资源不发布。

## 命令与验证

```sh
JAVA_HOME="$JDK" "$GHIDRA/support/analyzeHeadless" "$ISOLATED_PROJECT" bounded-20261026 \
  -process libg.so -readOnly -noanalysis -scriptPath "$PRIVATE_SCRIPTS" \
  -postScript NashuraFinishAudit.java "$PRIVATE_OUTPUT"
# exit 0; verify script log, executable SHA and decompileCompleted
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$MATCHED_UPDATE" --data-path / \
  --search-root "$MATCHED_UPDATE" --factory-mode register --resource-manifest "$MATCHED_MANIFEST" \
  --asset-root "$DATA" --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 \
  --catalog-mode loader --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$LOOKUP_TARGETS" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper nonzero; native/GDB exit 0, status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0; bounded observation
python tools/pure_linux/audit_finish_lookup.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0; bounded comparison
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1026 passed, 1 skipped
```

Android结果 SHA `f3f7dff62812d58621c5fb86fbcc26f93c0e072def682362fb42e00fec45ddb8`；
Linux结果 SHA `289b442314914544e2e3d6139d38bf28f488b59f112f71f34dee4206ab4fdf7c`；
GDB观察 SHA `08424b9f301075ea0111e9247f25e433cc08182628c4d45f604b4d99094aaa62`；
脱敏审计 SHA `9c22715f2e0e9219cc0dd46bc9a5d187c46cdb6d16ad1527a09928e7ead307ba`；
私有反编译输出 SHA `f4c700b121a6d651f12ce182ae064d35f3b32e82267320103fbd56a2446f2b0d`。
真实引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。

下一步在相同身份、清单及作用域下捕获查询键及输入对象初始化路径，逐步验证
Linux空返回来源。配置/Backup等价与严格catalog门未闭合；ResourceManager、
GameState、Step、完整对局和性能验收保持 NOT_RUN。

[机器证据](pure-linux-finish-lookup-evidence-20261029.json)。
