# E2.1-N：Android 与 Linux catalog 批量加载参数

基线 `143813c`。本阶段为 **RUNTIME_OBSERVED_LIMITED**：版本绑定的真实
Android 和 ARM64 Linux 独立进程各观察到5次批量加载；寄存器参数一致。
这不闭合17个表槽的行数差异，不通过严格 catalog 门。

## 实际观察

Android 独立 `e1-only` API30 进程在自然启动期间安装 Frida 观察点；已有 Linux
guest 的自有 libg 子进程在 post-load SIGSTOP 后由 GDB 跟踪调用指令处寄存器。
没有修改原始 libg 或写 ready。两侧真实 libg SHA 为
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。

Android `TableLoadBatch` 的 x2–x6 与 Linux 调用点实测一致，依次为：

```text
0 0 0   0 39
0 0 0  39 39
0 0 0  78 39
0 0 0 117 39
0 0 0 156 39
```

Android 每次列表计数均158；Linux 既有 probe 记录同样为158。Linux GDB inferior
exit0，行布局仍为162槽/24,353行，与未插 GDB 样本布局 SHA 相同；这只证明布局
摘要一致，不是完整轨迹或所有数据未受插桩影响。Linux wrapper exit1、配置严格
门关闭。Android runner v5 exit0、五次批量调用与已安装 hook 均有记录、观察错误
和丢事件均0；启动操作本身超时后取得唯一自有PID，resume返回invalid PID，实际
进程随后自然运行。早于 hook 的调用和未覆盖间接路径仍为 UNKNOWN，不能把零
postprocess hook 事件当作没有调用。

Android v5 update 前后为相同908项，是历史990项的内容相同子集；与 Linux 的990项
输入不完全一致。Android `filesystem_open` 的 `csv_logic/` 请求路径集合为597项，
既有 Linux trace 中 APK open/absent 与 update open 的路径并集也为597项，两边集合
差异0。这是跨进程、跨时间的路径集合，不是逐次调用配对，不证明打开有效、读取
内容、fallback 选择、顺序或解析后的表值一致。Android只在目标进程记录，其他
进程与未覆盖资源路径保持未知。

## 审计与失败保留

`audit_catalog_start_trace.py` 绑定 Android 结果与前后 update、历史清单、Linux
GDB结果及已有资源trace；复核双端五组参数、目标hook、进程退出与597项集合。
私有日志和原始路径不公开；独立摘要 SHA
`1397d5a4f29d0430406f6aa6a6a58c75563b3d7462926e98942619c9cf1182ee`，audit exit0。

首次 v1 Android 路径读取错误，2,750次资产请求的路径为未知，保留 result SHA
`655edd3f317d01e397cd66a5f1c4d820157ad09297f1d9306737d8791cdb7447`；v2 修正
路径读取后保存有效观察 SHA
`73d57577d002231364960e9f6a4da70cc76ec86cbf5d44c31d07e5fc9be28226`。
v3 Frida attach timeout exit1；v4 收尾连接卡住，核验后停止自有进程，runner exit1，
结果和两份清单保留；v5 改为先独占保存观察，再核验PID starttime后清理，exit0。
v4/v5 使用此前启动后已变为908项的隔离实例，不当作从990项启动的稳定输入。

## 实际命令

以下变量为私有路径；完整路径与原始日志留在本机隔离目录：

```sh
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$MATCHED_LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_V5"
# exit 0; capture requires audit, not a catalog pass
python3 provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / --search-root "$UPDATE" \
  --factory-mode register --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" --asset-backend "$BACKEND" \
  --asset-backend-sha256 "$BACKEND_SHA" --timeout 100 --catalog-mode loader \
  --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$PRIVATE_TARGETS" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper exit 1; GDB and inferior exit 0
python tools/pure_linux/audit_catalog_start_trace.py --android "$ANDROID_V5/result.json" \
  --android-before "$ANDROID_V5/update-before.json" --android-after "$ANDROID_V5/update-after.json" \
  --historical-update "$HISTORICAL_UPDATE" --linux-batch "$LINUX_OUT/result.json" \
  --linux-gdb "$LINUX_OUT/observation.json" --linux-resource-result "$PRIOR_RESULT" \
  --linux-trace "$PRIOR_TRACE" --linux-stderr "$PRIOR_STDERR" --identities "$PRIVATE_IDENTITIES" \
  --assets-root "$ASSETS_ROOT" --update-root "$UPDATE_ROOT" --cwd "$LINUX_CWD" --output "$AUDIT"
# exit 0: limited audit
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1004 passed, 1 skipped
```

`site_only` 观察在已核验 libg 的调用指令处读取寄存器，不在该位置添加错误的函数
返回断点。仍须跟踪资源解析和行构造/筛选，核实Android 908与Linux 990输入差异，
以及配置 Backup 和 catalog 内容。严格门通过后才进入后续战斗对象。
[机器证据](pure-linux-catalog-batch-trace-evidence-20261029.json)。
