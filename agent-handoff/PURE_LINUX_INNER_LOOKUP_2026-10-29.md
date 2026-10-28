# E2.1-N：catalog 内层查询双端观察

基线 `d660b8a6694c211671a6feb0d7aeeee88676079a`。本阶段对上一阶段保留的
真实 Android 与 ARM64 Linux/Bionic 实验结果做身份绑定审计，状态为
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 观察结果

- 两端的 908 项 update 清单逐项一致，Android 前后清单不变；Linux 输入、资源、
  辅助文件和 probe 前后身份不变。真实引擎 SHA-256 为
  `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 第五批 `finish_a` 前，已观察的 17 槽计数相同，合计 1,950 行；结束时
  Android 为 3,367 行，Linux 为 1,950 行。对应查询键均为 30 字节且逐字节一致。
- 在 `finish_lookup` 嵌套作用域内，版本绑定入口 `0x11ae1fc` 两端各一次；
  Android 返回非空，Linux 返回空。查询输入已观察的两层指针均非空。
- Android 钩子未报告脚本错误、丢事件或未关闭作用域；Linux 原生进程与 GDB
  均退出 0，但外层实验仍为 `BLOCKED`。这些是限定窗口运行观察，非完整初始化验收。

指针存在不等于对象或容器内容一致。自动反编译结果存在函数边界合并和 PIC 警告，
尚不足以证明容器语义、填充来源或差异的唯一原因。配置异常与 Backup 真实副作用
等价尚未闭合。首次 Android 采样的最终布局快照不可读，失败结果 SHA-256 为
`64ce0fe3c18b63a5ce9404dca34db8250199b68008ec65896284e0bcbe4dde6a`，原始记录保留。

## 验证

```sh
python tools/pure_linux/audit_lookup_inner.py --android "$ANDROID" --android-sha256 "$ANDROID_SHA" \
  --android-before "$BEFORE" --android-before-sha256 "$BEFORE_SHA" \
  --android-after "$AFTER" --android-after-sha256 "$AFTER_SHA" \
  --trace-script "$SCRIPT" --trace-script-sha256 "$SCRIPT_SHA" \
  --linux-result "$RESULT" --linux-result-sha256 "$RESULT_SHA" \
  --linux-observation "$GDB" --linux-observation-sha256 "$GDB_SHA" --output "$AUDIT"
# 首次传入的 SHA 参数不匹配：exit 1；复核各文件 SHA 后重新运行：exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1036 passed, 1 skipped
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
```

审计结果 SHA-256：`78655732510d193f80ccf2314eb23da9ea56cd7b9f42e5f231ffff71032370eb`。
原始运行结果、键原文、内存及私有日志保留在隔离目录，不进入公开仓库。
新增单元测试覆盖作用域错配、缺失返回、未知输入和不同查询键；它们检验审计器，
不代替原生语义验证。已关闭本实验专用 Android AVD。

下一步追踪该查询实际读取的数据结构及其填充路径，使用版本绑定的只读观测验证
假设。只有严格 catalog 门通过后才进入 ResourceManager、GameState 和 Step；
完整对局、无 JavaVM 及 10 倍性能均未验证。

[机器证据](pure-linux-inner-lookup-evidence-20261029.json)
