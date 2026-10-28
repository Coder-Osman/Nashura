# E2.1-N：catalog 批次 getter 返回序列对照

基线 `bafa37136f6e5cd05ce39b58eeb40c9400d22400`。本阶段为
**RUNTIME_OBSERVED_LIMITED**：独立 Android API 30 与 ARM64 Linux/Bionic 真实
`libg.so` 进程各观察 5 次批次，批次内候选 getter 返回序列逐项一致。
严格 `CATALOG_READY_PASS=false`，这不是表内容、完整引擎或无 JavaVM 验收。

## 实际结果

- 两侧批次内分别观察 158 次候选类型读取和 155 次候选 ID 读取，按批次分组
  为 `[76,78,78,78,3]`；313 项的调用类型与 32 位返回值按顺序一致。
  候选函数名称仅为分析标记，不推断所有返回值的完整业务含义。
- Android 使用只读独立 AVD，启动门控截获新建目标进程，恢复前安装模块观察器；
  所有目标 hook 安装成功、5 次批次均结束，Frida 脚本异常 0、事件丢失 0、
  活动作用域 0，门控已释放，实例已关闭。Linux GDB inferior exit 0、返回齐全、
  输入与资源身份不变；Linux wrapper 的结果状态仍为 BLOCKED。
- Android 此次 update 前后均 908 项，逐项属于历史 990 项清单；Linux 当前样本的
  已核验历史资源为 990 项。跨进程及输入差异仍限制比较，不能以 getter 一致
  解释 Android 25,810 行与 Linux 24,353 行的差异。
- 修复后的 Android 观察再次与既有 Linux 资源轨迹进行 SHA 绑定审计：597 个
  `csv_logic/` 路径的返回对象类别分别为 APK 230、update 116、空返回 251，
  与 Linux 已观察类别对应。不同进程、不同清单及对象类型并不证明读取字节、
  对象有效性、fallback 因果或解析出的表内容一致。

## 观察器修正及历史证据

Frida 回调上下文的 `threadId` 是只读属性；旧脚本在赋值时抛异常，但旧 runner
未注册脚本错误消息。新失败样本实际捕获 2,955 次文件入口及 5 次批次入口的
`TypeError`；旧资源对象报告中“观察错误为零”不足以排除该回调异常，原报告保留。
旧样本中路径在异常前读取、返回对象在异常后记录，但应按有限证据看待；本阶段
使用无脚本异常的新样本独立重验了上述 597 路径类别。

修复使用独立属性名，且 runner 显式记录并拒收脚本异常；目标 getter hook 在首个
批次入口安装，批次外覆盖为 UNKNOWN。新增门控避免 attach 晚于初始化；runner
等待指定数量的已结束批次，超时保留失败证据。测试包含只读 Frida 上下文、
缺失返回、序列错位、回调异常与证据篡改。门控仅用于本轮独占 AVD。

## 命令与身份

```sh
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5
# exit 0; RUNTIME_OBSERVED_REQUIRES_AUDIT, strict catalog false
python tools/pure_linux/audit_catalog_entry_sequences.py --android "$ANDROID_RESULT" \
  --android-sha256 "$ANDROID_SHA" --android-script "$SCRIPT" --android-script-sha256 "$SCRIPT_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --historical-update "$HISTORY" --historical-update-sha256 "$HISTORY_SHA" \
  --linux-result "$LINUX_RESULT" --linux-result-sha256 "$LINUX_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0; bounded comparison only
python tools/pure_linux/audit_catalog_resource_selection.py --android "$ANDROID_RESULT" \
  --android-before "$BEFORE" --android-after "$AFTER" --linux-result "$RESOURCE_RESULT" \
  --linux-trace "$RESOURCE_TRACE" --linux-stderr "$RESOURCE_STDERR" --identities "$IDENTITIES" \
  --assets-root "$DATA" --update-root "$UPDATE" --cwd "$CWD" --output "$RESOURCE_AUDIT"
# exit 0; bounded object categories only
node --check tools/pure_linux/android_catalog_start_trace.js
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1013 passed, 1 skipped
```

真实引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
Android 结果 SHA `eed0672161d1502a7ec76fb4e2b55ace093b1432b3c5b3afc0da54c9ddf6b990`；
getter 独立摘要 SHA `439a1bebb309597d47976e8280cb1983e430953095ea831023daa2e409d493cf`；
资源类别摘要 SHA `9b20450595c7690fbdc7881134b38fa2cd964a7a3bd48f52a4edea0413c46e2b`。
带异常的失败样本 SHA `b21eb1f136169abdbd5ce4deb9023022d19ca4cf10189e2aac6cff09a596c968`。
本次真实运行的 runner SHA `045403880622bc411f5ff3914e4ac5233af8cfcd7996780a7d95c4323bca7e41`：
运行后对门控清理异常路径另作加固，当前文件 SHA 以仓库版本为准。
原始值、资源、地址及私有日志不发布。

## 下一步

对同一来源的已读取资源与行构造/筛选分支开展版本绑定的逐阶段对照，首先厘清
Android 908 与 Linux 990 update 清单差异是否影响目标表。配置异常和 Backup
Linux 绑定仍未闭合；ResourceManager、GameState、Step 和完整对局保持 NOT_RUN。

[机器证据](pure-linux-catalog-entry-comparison-evidence-20261029.json)。
