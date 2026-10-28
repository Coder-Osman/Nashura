# E2.1-N：对齐 update 清单后的 catalog 行差异

基线 `b9152ab26970f37a14013dfd56cc27e62c9cdf1f`。本阶段为
**RUNTIME_OBSERVED_LIMITED**：独立真实 Android 与 ARM64 Linux 进程以逐项相同的
908 项 update 文件启动并观察布局；严格 `CATALOG_READY_PASS=false`。

## 结果及边界

- Android 采集前后 update 清单相同，908 项逐项 SHA 与本次 Linux 副本一致。
  两次只读快照相等，目标引擎 SHA 与 Linux 一致；Frida 脚本异常、丢事件、活动
  作用域均为 0，目标批次记录齐全。结果为 162 槽、25,810 行。
- Linux 在新建独立副本上运行真实 libg，原生退出码 0、无超时，输入、probe 与
  资源前后不变；运行器返回非零，结果状态为 BLOCKED。结果为 162 槽、24,353 行。
- 与同 probe、同引擎输入的原 990 项 Linux 运行对照，162 槽布局及已覆盖的
  2,078 个差异槽 `row+0x40` 字段逐项一致。两平台的 17 个差异槽中，Android
  3,535 个字段值包含 Linux 的 2,078 个，Android 多 1,457 个，Linux 额外 0 个。
  该偏移在所有对象类型上的语义仍未确认，字段包含关系不证明其他对象内容相同。
- 因此，单独改变本次 Linux update 文件集合没有改变上述已观察布局；此证据
  不排除其他资源、实际读取、初始化顺序或平台依赖对差异的影响，也不证明完整
  catalog 内容一致。两侧进程及运行时仍不同。

## 输入及失败记录

新工具只把 Android 清单中存在的文件复制至独占目录，复制前后核验源文件与副本
的逐项 SHA、目录及剩余磁盘空间；生成 `data/assets/search/update` 四命名空间
清单，不修改既有990项目录。908文件共145,762,975字节。

首次预检因工具错误假定只有两个命名空间而失败，未创建资源副本；错误日志 SHA
`a62288bf9f92cb5f5acad94f157f1750b111d010aa01be502896c7242209df9e`。当次
包装脚本的退出码文件错误记录为0，以实际 traceback 为准；修复后在新目录完成复制，
之后的包装改用显式成功/非零分支。合成单元测试覆盖哈希篡改、链接、命名空间与
磁盘空间拒收，不当作真实引擎结果。

Android 独占只读 AVD 的预热运行清单从990项变为956项，之后只读检查为908项；
文件变化的具体操作及来源仍未知。正式捕获在908项稳定状态开始和结束，保持原生
初始化顺序；不以该现象推断删除因果。实例和观察服务已退出。

## 命令与身份

路径变量代表私有隔离输入；不发布资源或原始日志。

```sh
python materialize_update_subset.py --source "$UPDATE_990" --inventory "$ANDROID_908" \
  --inventory-sha256 "$ANDROID_908_SHA" --manifest "$MANIFEST_990" \
  --manifest-sha256 "$MANIFEST_990_SHA" --output "$ISOLATED_COPY"
# exit 0; 908 files, exact hash matches
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$ISOLATED_COPY/resources" --data-path / \
  --search-root "$ISOLATED_COPY/resources" --factory-mode register \
  --resource-manifest "$ISOLATED_COPY/resource-manifest.json" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" --asset-backend "$BACKEND" \
  --asset-backend-sha256 "$BACKEND_SHA" --timeout 55 --resource-trace --catalog-mode loader \
  --inspect-parsed-tables --inspect-row-ids --expected-preload-requests 377 --native-backup-queue \
  --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper nonzero, native exit 0, status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --layout-script tools/pure_linux/android_catalog_layout.js --inspect-row-ids
# exit 0; bounded capture, not catalog parity
python tools/pure_linux/audit_matched_update_layout.py \
  --android "$ANDROID_RESULT" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$TRACE_SCRIPT" --trace-script-sha256 "$TRACE_SHA" \
  --layout-script "$LAYOUT_SCRIPT" --layout-script-sha256 "$LAYOUT_SHA" \
  --old-result "$OLD_RESULT" --old-result-sha256 "$OLD_RESULT_SHA" \
  --old-stdout "$OLD_STDOUT" --old-stdout-sha256 "$OLD_STDOUT_SHA" \
  --new-result "$NEW_RESULT" --new-result-sha256 "$NEW_RESULT_SHA" \
  --new-stdout "$NEW_STDOUT" --new-stdout-sha256 "$NEW_STDOUT_SHA" --output "$AUDIT"
# exit 0; differences retained
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1019 passed, 1 skipped
```

Android result SHA `5d5cb425fc219f73010444b9426b7e821824845e862023e127a9e1f360194dd5`；
Linux 908 result SHA `57ee2e24c4ff3fd21067e074383f6453e7b4d8754427fe83897eb10c8d2ba918`；
独立审计 SHA `b2e28228d47457605b1701b055ced2502293f7beb13777e9b2d465b1ff083d3c`；
资源复制结果 SHA `d7b14b4dd0639be9aff0da9b0e4d2b881b8d7b1c8562e3db4e7f0fb3d34af933`。
原始字段值、APK、资源及进程日志保持私有。

## 下一步

对版本绑定的表创建及后处理路径开展分阶段观测，确认行数差异从哪一步开始；
继续闭合配置异常与 Backup 等价。ResourceManager、GameState、Step、完整对局及
性能验收仍 NOT_RUN。

[机器证据](pure-linux-matched-update-layout-evidence-20261029.json)。
