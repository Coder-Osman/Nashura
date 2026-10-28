# E2.1-N：数据库所属对象构造的限定观察

基线 `3bd433f8bef6d0a94c08adb864bae4894753028c`。状态为
`RUNTIME_OBSERVED_LIMITED`；严格 `CATALOG_READY_PASS=false`。

在独立 Android ARM64 只读 AVD 的自然启动中，版本绑定的获取候选 `0x7557d0`
由返回地址 `0x13a43e0` 调用；其作用域内，构造候选 `0x755a98` 由返回地址
`0x755804` 调用并返回，返回后对象的虚表匹配 `0x17ea568`。该获取调用返回的
地址与随后 `0x75760c` 数据库初始化使用的地址相同，均在同一线程。关联期间未
观察到同地址的第二次构造；这不是完整生命周期或地址释放跟踪。

真实库上只读反汇编显示，候选获取入口在缓存为空时请求 `0x530` 字节并调用
构造候选；上游静态指令将返回值存入持有对象，再读取虚表项进行调用。内存分配、
调用参数和完整对象所有权尚未做运行时验证；静态调用点本身不证明语义。

本轮首次预热的 update 清单从 990 变为 930 项，保留为非稳定观察，不用于一致性
判断。第二次与增加获取入口 hook 后的第三次均为 908 项、前后身份一致；第三次
脚本错误和丢事件均为零。两次均使用同一只读临时 AVD，未改变原始 APK 或引擎。
本阶段没有执行配置 store 或 Backup 请求；此前已授权的一次写入及恢复见
`PURE_LINUX_RUNTIME_PREFERENCE_BINDING_2026-10-28.md`。

## 验证

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 三次 exit 0；首次清单漂移，后两次前后均为 908 项
python tools/pure_linux/audit_owner_constructor.py --result "$ANDROID_OUT/result.json" \
  --result-sha256 "$RESULT_SHA" --script tools/pure_linux/android_catalog_start_trace.js \
  --output "$PRIVATE_AUDIT"
# exit 0
python -m pytest -q -p no:cacheprovider tools/pure_linux/test_audit_owner_constructor.py \
  tools/pure_linux/test_audit_database_owner.py tools/pure_linux/test_audit_database_factory.py
# exit 0，12 passed
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1074 passed，1 skipped
python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 1，1057 passed，2 skipped，16 failed；系统 Python 缺少 cryptography
python3 tools/check_public_snapshot.py
# exit 0
gitleaks dir . --config .gitleaks.toml --redact
# exit 0，未发现泄漏
```

输入、输出及预热记录的 SHA 见[机器证据](pure-linux-owner-constructor-evidence-20261029.json)。
原始追踪含资源路径，保存在 `/private/tmp/nashura-owner-constructor-android-20261029-v1`、
`v2`、`v3`，未提交。此观察未证明插桩前后完整轨迹一致；Linux 构造器窗口仍未覆盖。
下一步在隔离 Linux 子进程验证真实上游入口及参数、资源依赖和对象生命周期，并继续
闭合配置异常和 Backup 副作用。ResourceManager、GameState、Step 均为 `NOT_RUN`。
