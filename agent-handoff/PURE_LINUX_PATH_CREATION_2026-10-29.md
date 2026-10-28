# E2.1-N：目标树路径构造的双端观察

基线 `0d1228f4d11f8439199a480ca47ab8f61eb63ed3`；本阶段状态为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 观察与限制

在匹配真实引擎和两侧逐项一致的 908 项 update 清单下，Android 对前一阶段
目标树的查询之前，路径构造候选 `0x11afc60` 在该对象上有 14,653 次调用，
全部返回；按路径字节与调用者聚合为 1,763 项，调用返回位置均为 `0x11b08e4`。
其中 1,871 次调用、448 个聚合项的第一路径分量与最终查询键相同；没有完整
路径字节等于该查询键。路径原文和内存仅保留在私有目录。

Linux 真实原生进程在 post-load 观察窗口中，该入口全局调用为 0，目标树根空；
原生进程及 GDB exit 0，运行器状态为 `BLOCKED`。Linux 构造器未被观察，Android
路径记录从模块观察钩子安装后至目标查询前，因而不证明全生命周期或唯一因果。
静态指令显示 Android 的返回位置位于 `0x11b0820` 附近候选函数；其业务语义和
路径输入的产生来源仍待动态证据。未验证路径字节与资源文件的一一对应关系。

首次采样对每个对象仅保留 4,096 条记录，触发 10,557 次上限错误，且 update
清单从 990 项变为 930 项，不作为合格对照；失败结果 SHA
`6905aa5f68d584a6c3b803ab571560e7a3b9e567acf82fc3e81a45f80547c542`。
随后改为按调用者与字节聚合，保留调用和返回计数并对不同项设上限；正式采样的
脚本错误和丢事件均为 0，update 前后均为 908 项。聚合不证明探针对完整轨迹无影响。

## 命令与身份

```sh
# Linux 使用前一阶段同一 provider_job 参数与隔离输入，改用
# --observe-targets tools/pure_linux/path_creation_targets.json；
# native/GDB exit 0，严格状态 BLOCKED。
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# exit 0，908 update 文件前后不变
python tools/pure_linux/audit_path_creation.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1054 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

真实引擎 SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`；
审计 SHA `62af19de95d14594c9d819ce4651b3c37393de87cc466a5ed7454d19d935a3d8`。
完整证据身份见[机器证据](pure-linux-path-creation-evidence-20261029.json)。
实验专用 AVD 已关闭。下一步追踪上游 `0x11b0820` 的调用者与路径生成，
验证 Linux 缺失调用的具体条件；配置、Backup、完整 catalog、战斗生命周期及
性能门均未通过。
