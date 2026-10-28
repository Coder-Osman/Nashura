# E2.1-N：catalog 查询首段及树状态

基线 `04d8fa55f1bf74063a7f935c7a9a3ceff21208ab`。真实 Android 和 ARM64
Linux/Bionic 进程的受限观察经身份审计，状态为 `RUNTIME_OBSERVED_LIMITED`；
严格 `CATALOG_READY_PASS=false`。

## 结果

匹配版本的指令静态显示，内层入口使用路径首段查找对象 `+0xf8` 附近的结构，
将结果与 `+0x100` 哨兵比较。此解释绑定真实库 SHA，仍不是完整结构或业务语义证明。
两次真实运行的 908 项 update 清单逐项一致、输入资源及 probe 前后未改变；两侧
30 字节查询键相同。通过版本绑定和作用域校验后：

| 局部观察 | Android | Linux |
|---|---:|---:|
| `+0x100` 根指针非空 | 是 | 否 |
| `+0x108` 机器字 | 8 | 0 |
| 首段查找命中 | 是 | 否，返回哨兵 |
| 内层最终结果非空 | 是 | 否 |

Android 在同一作用域内一共观察到六次树查询，表内仅取第一项；Linux 第一次
未命中后退出。相邻机器字是未经完整证明的结构字段，不将其当成已验证元素数。
本观察不证明树如何填充、为何为空或该差异是唯一原因。Android 控制脚本 exit 0；
Linux 原生进程和 GDB exit 0，外层状态 `BLOCKED`。两端为独立运行时，探针前后
并未进行完整轨迹等价证明。

## 失败与探针边界

Android 尝试 v1、v2 和 v3 使用函数中途插桩，目标进程报 `std::bad_alloc` 并
中止；v1、v2 的实验输出 SHA 分别为
`e453d571f76dd1befefda47f0761a7d7ece93fb78c75bdd22d274a4ef9588b4a`、
`36398c74f7c27a7497817b9a488fe300af8ca1d960723d9cf23e0f0118259b3f`。
v3 SHA 为 `60569bb09cd95bd27d94d05b156491780ed7279ed0851935dbe61a080056c188`。
探针影响是可能解释，尚未单独证明唯一原因。旧脚本控制实验进程存活且观察到
5 批，但因为当时将本机控制端口错误转发至设备同号端口，超时结果不构成初始化
失败证据。该失败记录 SHA 为
`26c359337f4f854a5236833d5cb51f17d04ccedaaf1b7477e31ee5db23f3b4af`。
随后将设备端口修正为 probe 的 `26789`，改用完整函数入口/返回 hook，v4 成功。
所有失败结果保留在本地隔离目录；私有日志、内存和原始库不发布。

## 命令与身份

```sh
# 在 Ubuntu aarch64 隔离目录执行，与上一轮相同的资源、配置副本和加载器
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$NEW_OUT" \
  --mode both --data-root "$DATA" --update-root "$MATCHED_UPDATE" --data-path / \
  --search-root "$MATCHED_UPDATE" --factory-mode register --resource-manifest "$MANIFEST" \
  --asset-root "$DATA" --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 \
  --catalog-mode loader --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets first_lookup_targets.json --preference-material "$MATERIAL" \
  --preference-material-sha256 "$MATERIAL_SHA" --preference-xml0 "$XML0" \
  --preference-xml0-sha256 "$XML0_SHA" --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# native/GDB exit 0; status BLOCKED
adb -s emulator-5630 forward tcp:28996 tcp:26789
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0; 908-entry inventory unchanged
python tools/pure_linux/audit_first_lookup.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1043 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

审计摘要 SHA `1bdf2a165660796e0a4841b4cfcb73af40b888f1a66829fcd88368e07e341b82`。
其他输入和输出 SHA 见[机器证据](pure-linux-first-lookup-evidence-20261029.json)。
专用只读 AVD 已关闭；下一步追踪对象构造、树插入与资源/初始化来源。配置异常、
Backup 等价及完整 catalog 门仍未闭合；ResourceManager、GameState、Step、完整
对局、无 JavaVM 验收及 10 倍性能均未运行。
