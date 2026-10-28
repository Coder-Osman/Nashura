# E2.1-N：catalog 完成分支的行计数边界

基线 `5e3e7dc6107e9123fe8799234495798cc5bfeb7e`。本阶段以匹配的 908 项 update 清单，在独立 Android 和 ARM64
Linux/Bionic 的真实 `libg.so` 进程中观察第5批内两个版本绑定的候选调用。
状态为 **RUNTIME_OBSERVED_LIMITED**，严格 `CATALOG_READY_PASS=false`。

## 观察结果

| 候选边界 | Android 17槽行数 | Linux 17槽行数 | 不同槽数 |
|---|---:|---:|---:|
| `finish_a` 进入 | 1,950 | 1,950 | 0 |
| `finish_a` 返回 | 3,367 | 1,950 | 17 |
| `finish_b` 进入 | 3,367 | 1,950 | 17 |
| `finish_b` 返回 | 3,367 | 1,950 | 17 |

两个入口均在双方第5批中各调用一次，进入和返回记录完整。Android 受控启动、
脚本错误0、事件丢失0、活动作用域0，update采集前后均为相同908项；Linux
GDB和原生子进程退出0，资源与输入前后不变，运行器状态 BLOCKED。
Linux最终布局摘要与此前未做该阶段插桩的样本一致，不能据此证明完整轨迹未变。

`finish_a` 是实验观察名称，绑定 ELF 偏移 `0xe1c290`；调用点的静态指令将
context传入，运行期表读取又核验了归属、槽类型及计数边界。`finish_b` 绑定
偏移 `0xdcd238`。这些名称不声明函数业务语义；1,417行差异出现在 `finish_a`
进入到返回的区间，不能仅凭边界快照排除并发线程的影响，也不能单独断言内部
某个具体分支是原因。

此前最终快照的额外40行差异出现在该批返回与最终快照之间，尚需单独调查。此处
只覆盖选定17槽的计数，不证明全部表值、输入消费、配置或 Backup 等价。

## 工具与验证

Android hook 只在批次作用域记录两个候选入口；Linux GDB 使用相同 context寄存器
及作用域读取。独立审计校验输入SHA、目标偏移、调用顺序、返回、线程和相邻
边界连续性；原始内容、地址及游戏文件不公开。隔离 AVD 和服务已关闭。

```sh
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$MATCHED_UPDATE" --data-path / \
  --search-root "$MATCHED_UPDATE" --factory-mode register --resource-manifest "$MATCHED_MANIFEST" \
  --asset-root "$DATA" --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 \
  --catalog-mode loader --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$PHASE_TARGETS" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper nonzero; native/GDB exit 0, strict status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0; bounded observation
python tools/pure_linux/audit_catalog_finish_phases.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$TRACE_SCRIPT" --trace-script-sha256 "$TRACE_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0; bounded phase comparison
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1024 passed, 1 skipped; tightened auditor tests rerun: 2 passed
```

Android result SHA `645b135a3b5a0bd20af423ea952ff1d1440275452d489e519790c82785bbc177`；
Linux result SHA `5004e55303a730b70711e6c9d55f064822e1506c52dd2b636078c11f26948f24`；
GDB observation SHA `5e95b73a57a05cff507c11a7a00938a00783e74a43c97169b050794635cebcda`；
审计 SHA `cd2ada8f8c279bf23211f965e657781b838217304e7d6afec3376d49ecefda6a`。
真实 libg SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。

## 下一步

在版本绑定分析库中审查 `finish_a` 的内部控制流及真实调用，再设计双端只读
观测，以区分条件、输入与依赖。严格 catalog 和 Backup 门通过前，ResourceManager、
GameState、Step、完整对局与性能验收保持 NOT_RUN。

[机器证据](pure-linux-catalog-finish-phases-evidence-20261029.json)。
