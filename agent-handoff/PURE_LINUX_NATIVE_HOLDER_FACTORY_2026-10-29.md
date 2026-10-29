# E2.1-N：Linux 真实持有者工厂

基线 `0d4ff1a`。状态 `NATIVE_VERIFIED_LIMITED`；严格
`CATALOG_READY_PASS=false`。

Android 匹配库自然启动的稳定观察表明，工厂 `0x13a29b0` 由返回地址
`0x139dd10` 调用，使用与先前观察一致的分配器创建包装对象及其关联对象。
后者成为持有者工厂的第二参数，其首指针对应 `libg.so` 偏移 `0x18c8b40`，
并与上游对象 `+0xe8` 字段相同。分配器构造及参数来源在一个线程，持有者
工厂在另一线程；地址关联未证明完整释放或跨线程生命周期。

在 Ubuntu ARM64 guest 独占子进程中，以真实库构造经验证的分配器，调用真实
参数工厂 `0x13a29b0` 和持有者工厂 `0x13a3d14`。两者返回非空对象，检查
对应虚表、分配器字段、参数指针和空初始化槽；随后调用真实析构函数和
分配器释放，并检查分配器 canary。原生进程 exit 0，输入、资源、probe 和
辅助文件身份未变。没有调用持有者设置、数据库初始化或战斗入口。构造采用
独立栈内分配器、单线程和 strace，不能等同原始父对象生命周期或完整轨迹。

Android 首次预热 update 清单由 990 变为 933 项，最终复测前后均为 908 项；
v3 追踪曾读取错误偏移并产生访问异常，该失败已保留，修正后 v4 无脚本错误
和丢事件。专用 AVD 已关闭。Linux 外层仍为 `BLOCKED`，配置异常和 Android
Backup 等价未闭合，严格 catalog 门未通过。

## 验证

```sh
node --check tools/pure_linux/android_catalog_start_trace.js
# exit 0
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT" \
  --spawn-gate --expected-batches 5 --batch-table-counts --batch-phase-counts \
  --layout-script tools/pure_linux/android_catalog_layout.js
# 最终稳定运行 exit 0，update 前后各 908 项
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread -Wl,--export-dynamic \
  tools/pure_linux/native_init_probe.c tools/pure_linux/provider_probe.c \
  tools/pure_linux/preference_store_hook.c tools/pure_linux/preference_store.c \
  tools/pure_linux/preference_files.c tools/pure_linux/preference_contract.c \
  tools/pure_linux/preference_codec.c tools/pure_linux/preference_cipher.c \
  tools/pure_linux/native_backup_queue.c -ldl -o "$PROBE"
# exit 0
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$OUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / \
  --search-root "$UPDATE" --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" \
  --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA" \
  --native-backup-queue --holder-factory-only --timeout 120
# 原生 exit 0，外层 exit 1 / BLOCKED
python tools/pure_linux/audit_native_holder_factory.py --android "$ANDROID_RESULT" \
  --android-sha256 "$ANDROID_SHA" --script tools/pure_linux/android_catalog_start_trace.js \
  --linux-dir "$LINUX_COPY" --output "$PRIVATE_AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1094 passed，1 skipped
```

身份见[机器证据](pure-linux-native-holder-factory-evidence-20261029.json)。原始日志
位于 `/private/tmp/nashura-factory-arg-android-20261029-v1` 至 `v4` 和
`/private/tmp/nashura-linux-holder-factory-20261029-v1`；guest 新目录为
`/tmp/nashura-linux-holder-factory-20261029-v1`。下一步验证真实持有者设置入口的
前置状态和销毁路径，再逐项追踪初始化中的资源和配置；ResourceManager、
GameState、Step、完整对局及性能验收均未运行。
