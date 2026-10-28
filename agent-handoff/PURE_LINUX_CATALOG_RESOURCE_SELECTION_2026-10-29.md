# E2.1-N：catalog 逻辑资源实际对象类型对照

基线 `505b635b484a0939dba2155901dd67306e77cc7d`。本阶段为
**RUNTIME_OBSERVED_LIMITED**，比较独立 Android 与 Linux 真实引擎进程中
`csv_logic/` 路径对应的文件对象类别；不通过严格 catalog 门。

## 结果及边界

Android `filesystem_open` 按返回对象虚表分类：APK 对象230、磁盘对象116、空返回251。
逐路径与先前真实 Linux loader 的 APK backend 打开、update 文件打开和 APK 未命中
分别对应；597项中类别差异0，Linux 无额外 `csv_logic/` 观察路径。

这是两个不同时间、不同进程的路径关联，不是同一次请求的跨平台因果追踪。APK
非空对象可能是无效包装，本次未调用 valid 虚函数；Android 空返回与 Linux APK
未命中只表示各自观察到的结果，不证明所有 fallback/错误分支一致。返回类型也不
证明读取字节或解析值一致。

Android 采集前后 update 分别990和908项；82项变化的时点和删除来源未知。
事件上限未触发、观察错误0、活动 scope 0；资源路径和虚表只记录目标进程中
已安装的观察点。AssetManager 调用未与 filesystem scope 建立同线程父子关系，
因此未声称其调用者或顺序因果。原始日志和资源名称留在私有目录。

## 身份与验证

- Android result SHA `5c2909e439b0fc2fe6559889772de9532f42eb9189b806a8ecf29c8f9f6d4960`。
- Android update before/after SHA `411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942` / `47d4ad200eafff21ea1538132e2bb629526efd87e495d72baf3acb9d95f9e2bc`。
- Linux result/trace/stderr SHA `caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014` / `0a34bb830c6e805de966479b0cb8276b7a8b6068d76cd440b32c8c6b758ca071` / `d17169692bc04fada60abc7fe686c1ac4abbdcd58589e89ceaa7edf22d45b875`。
- 独立摘要 SHA `653b2ab029c7b1474a880ed7ebac52776e63cb7e44a5a101958b460cb9effb6d`。

真实运行使用现有 `e1-only` API30 ARM64 的只读实例，APK/libg身份核验后启动自有
目标进程；未提交动作或配置 store，实例结束后关闭。命令中的变量为私有路径：

```sh
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$MATCHED_LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_RUN"
# exit 0; update identity changed, observation only
python tools/pure_linux/audit_catalog_resource_selection.py --android "$ANDROID_RUN/result.json" \
  --android-before "$ANDROID_RUN/update-before.json" --android-after "$ANDROID_RUN/update-after.json" \
  --linux-result "$LINUX_RESULT" --linux-trace "$LINUX_TRACE" --linux-stderr "$LINUX_STDERR" \
  --identities "$PRIVATE_IDENTITIES" --assets-root "$ASSETS_ROOT" --update-root "$UPDATE_ROOT" \
  --cwd "$LINUX_CWD" --output "$PRIVATE_AUDIT"
# exit 0: bounded comparison, not catalog pass
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1006 passed, 1 skipped
```

独立审计重验现有Linux完整资源trace、输入SHA及Android观察状态；合成测试仅覆盖
分类、重复和未知拒收。下一步追踪相同资源路径的解析与对象行生成，继续核实
update变化、配置与Backup契约。`CATALOG_READY_PASS=false`，ResourceManager、
GameState、Step、完整对局和10×未运行。

[机器证据](pure-linux-catalog-resource-selection-evidence-20261029.json)。
