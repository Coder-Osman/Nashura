# E2.1-N：catalog 行数差异的批次边界

基线 `26ab5cea51ea5aecd670bae2d39f10a61d9d5b8f`。本阶段对独立 Android 与 ARM64
Linux/Bionic 的真实 `libg.so` 运行进行只读观测；双方 update 清单逐项相同，均为
908 项。状态为 **RUNTIME_OBSERVED_LIMITED**，严格 `CATALOG_READY_PASS=false`。

## 实际结果

在此前发现有差异的17个表槽，分别于每个批次入口及返回读取表计数：

| 边界 | Android 合计 | Linux 合计 | 计数不同槽数 |
|---|---:|---:|---:|
| 第1批开始 | 0 | 0 | 0 |
| 第1批结束 | 502 | 502 | 0 |
| 第2批结束 | 553 | 553 | 0 |
| 第3批结束 | 610 | 610 | 0 |
| 第4批结束及第5批开始 | 611 | 611 | 0 |
| 第5批结束 | 3,367 | 1,950 | 17 |
| 最终布局快照 | 3,535 | 2,078 | 17 |

因此，这17槽的首次已观察分歧出现在**第5批内部**，该批返回时差1,417行；
从第5批返回到最终布局快照，两侧分别再增加168和128行，最终差1,457行。
只观察批次边界，尚不能确定具体哪次内部调用引入差异，或这些计数变化的因果。

Android 从本轮专用只读 AVD 的受控 spawn 开始记录；5次批次的开始和结束均捕获，
脚本异常、事件丢失与活动作用域均为0，update清单前后相同。Linux 使用匹配
SHA的独立真实引擎和 908 项副本，GDB只附加本轮子进程；5次批次均有前后计数和
返回，原生退出0，运行器状态 BLOCKED。Linux 最终布局摘要与未做该批次插桩的
样本一致；这不证明完整执行轨迹未受插桩影响。

每个非空表槽均核验 context 归属、槽类型、容量和行数边界；空槽保持独立标记。
快照只包含所列17槽的计数，不代表所有表、字段、资源或行为一致。两侧仍是不同
进程和平台运行时，配置异常及 Backup 等价未闭合。

## 工具与命令

`catalog_table_counts.py` 为已暂停子进程做有界读取；GDB 目标按引擎 SHA、
指定入口和 context 寄存器绑定。Android 批次 hook 可选记录入口和返回快照，
独立审计器校验批次顺序、返回、逐项输入身份及连续边界。合成测试覆盖只读表
不变量、缺失返回及批次间未观察变化拒收，不当作原生证据。

```sh
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$MATCHED_UPDATE" --data-path / \
  --search-root "$MATCHED_UPDATE" --factory-mode register --resource-manifest "$MATCHED_MANIFEST" \
  --asset-root "$DATA" --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 \
  --catalog-mode loader --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$BATCH_TARGET" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper nonzero; native/GDB exit 0; strict status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0; limited capture
python tools/pure_linux/audit_catalog_batch_counts.py \
  --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$TRACE_SCRIPT" --trace-script-sha256 "$TRACE_SHA" \
  --layout-script "$LAYOUT_SCRIPT" --layout-script-sha256 "$LAYOUT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" \
  --linux-stdout "$STDOUT" --linux-stdout-sha256 "$STDOUT_SHA" --output "$AUDIT"
# exit 0; bounded count comparison
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1022 passed, 1 skipped; tightened auditor tests rerun: 2 passed
```

Android result SHA `d6b473d33b4a4f334bc7244e0c8cc21385e7aa08d26e10aed2c627a9e49af5e7`；
Linux result SHA `e36189fd1e16447d4d85d55845c6b2b3da863b4ca44aafb438fe2b48415fdc6a`；
GDB observation SHA `427661312d6bf22e7e1c78e5c887845c215a9e658f918aa40427663e10d4f167`；
独立审计 SHA `6750c3581bae88b2f3e0f920601df337dcda38b1935850f9d676bfac6fb15ec3`。
引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
原始日志、内存地址、APK及资源保持私有；本轮专用 AVD 已关闭。

## 下一步

沿版本绑定的第5批完成分支，对其内部候选调用做独立前后计数观察；之后再调查
批次返回到最终快照之间的行增长。不得依据静态伪代码单独确定原因。严格 catalog
与 Backup 门通过前，ResourceManager、GameState、Step、完整对局均 NOT_RUN。

[机器证据](pure-linux-catalog-batch-boundaries-evidence-20261029.json)。
