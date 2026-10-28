# E2.1-N：上游持有者与分配器

基线 `25e6b35`，状态 `RUNTIME_OBSERVED_LIMITED`；严格
`CATALOG_READY_PASS=false`。

匹配 `libg.so` 的只读静态指令显示，候选工厂 `0x13a3d14` 使用其输入对象的
虚方法分配持有者，并将输入写入持有者 `+8`。随后候选设置入口 `0x13a3e3c`
使用该分配器，构造并写入持有者 `+0x20`。这些结构解释仍需 Linux 运行时验证。

隔离 Android 实例自然启动中，已观察同线程链：工厂返回持有者，持有者 `+8`
与工厂分配器输入相同；设置入口由返回地址 `0x139e330` 调用，进入时 `+0x20`
为空，返回时非空；后续已观察的对象初始化使用该字段。分配器首指针对应
`libg.so` 偏移 `0x18bf798`。这是单次版本绑定运行时观察，不证明分配器完整
生命周期、所有参数或初始化顺序对其他环境相同。

首次冷启动 update 清单从 990 项变为 908 项，仅作预热保留。稳定复测前后
均为 908 项，脚本错误及丢事件为零；未进行完整无插桩轨迹比较。专用只读 AVD
已关闭，未修改原始引擎文件。Linux 中该分配器是否已构造仍为未知；本轮未用
伪造对象调用后续初始化。

## 验证

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 稳定运行 exit 0，资源清单前后各 908 项
python tools/pure_linux/audit_holder_factory.py --result "$ANDROID_OUT/result.json" \
  --result-sha256 "$RESULT_SHA" --script tools/pure_linux/android_catalog_start_trace.js \
  --output "$PRIVATE_AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1084 passed，1 skipped
```

引擎、追踪、预热及审计 SHA 见[机器证据](pure-linux-holder-factory-evidence-20261029.json)。
原始观察位于 `/private/tmp/nashura-holder-setup-android-20261029-v1` 至 `v4`，未发布。
下一步识别该分配器的构造、上下文与释放契约，核实 Linux 实际对象后逐步执行
持有者工厂；配置异常与 Backup 等价仍未闭合。ResourceManager、GameState、Step、
完整对局和性能评估均为 `NOT_RUN`。
