# E2.1-N：对象初始化参数与上游持有者

基线 `5075fc6`。状态 `RUNTIME_OBSERVED_LIMITED`；严格
`CATALOG_READY_PASS=false`。

在独立 Android ARM64 只读实例的自然启动中，真实 `libg.so` 的虚表初始化入口
`0x756be8` 由返回地址 `0x13a1c88` 调用，入口对象关联此前观察到的构造实例。
其第二参数非空且可读，等于对象偏移 `+8` 的原有字段；同时等于对象获取函数
调用时版本绑定的 `x19` 持有者的 `+0x20` 字段。初始化返回时两者仍相同。
同一线程、同一初始化作用域内进入并返回数据库初始化候选 `0x75760c`。

只读静态指令显示，持有者的两个相邻字段被装入 `x0/x1`，随后通过虚表调用；
结果与运行观察一致。参数首机器字未指向当前已映射地址，故其类型和含义为
`UNKNOWN`；追踪文件中历史字段名 `argument_vtable` 是首机器字的映射分类，
不是将该字确认为虚表。持有者寄存器、字段偏移与结论仅适用于匹配 SHA 的库。
尚未追踪该参数的构造、析构或跨调用生命周期，也未证明两次启动的完整轨迹一致。

首次预热 update 清单从 990 变为 935 项；后续稳定复测前后均为 908 项，最终
追踪无脚本错误、丢事件或作用域不匹配。专用 AVD 已关闭；未修改原始文件，
未将私有路径、资源或内存内容提交。Linux 侧未以未知参数调用初始化函数。

## 验证

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 最终稳定运行 exit 0，update 清单 908 项前后一致
python tools/pure_linux/audit_owner_init.py --result "$ANDROID_OUT/result.json" \
  --result-sha256 "$RESULT_SHA" --script tools/pure_linux/android_catalog_start_trace.js \
  --output "$PRIVATE_AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1083 passed，1 skipped
```

引擎 SHA、追踪、预热及审计 SHA 见[机器证据](pure-linux-owner-init-argument-evidence-20261029.json)。
私有实验保存在 `/private/tmp/nashura-owner-init-android-20261029-v1` 至 `v5`。
下一步追踪持有者 `+0x20` 的写入来源与实际类型，完成 Linux 所需的原始前置对象；
继续闭合配置异常和 Backup 契约。ResourceManager、GameState、Step、完整对局和
性能评估仍为 `NOT_RUN`。
