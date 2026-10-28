# E2.1-N：catalog 批次内入口观测

基线 `1a528261becaa9aad2b2ba4f42cacd5fff8c7c43`。本阶段为
**RUNTIME_OBSERVED_LIMITED**：真实 ARM64 Linux/Bionic 进程的 catalog 批次内，
两个版本绑定候选 getter 的入口和返回得到记录。严格 `CATALOG_READY_PASS=false`，
此结果不证明 Android 对照或战斗生命周期。

## 观测及限制

- 5 次批量调用、158 次候选类型读取、155 次候选 ID 读取；318 次调用均有返回，
  GDB inferior exit 0、活动作用域为 0。观测到一个线程；其他时段及未设断点的
  路径保持 UNKNOWN。函数名和返回值的业务语义尚未验证。
- 输入、资源、probe 和辅助文件前后身份相同；表布局为 162 槽、24,353 行，
  与既有 Linux 样本的布局摘要一致。布局摘要不是全量表值或执行轨迹一致性证明。
- wrapper 的操作系统退出码 0，结果状态仍为 BLOCKED，严格 catalog 门为 false；
  运行器退出码不得当作验收门状态。
- 无作用域的首轮观察在 120 秒超时，进程退出 -9、无 observation 文件；日志停在
  catalog list 阶段。随后仅在批量函数作用域启用 getter 断点的运行完成。该对照
  支持缩小观测开销，但没有证明超时的唯一原因。
- Android 独立 AVD 两次启动后 Frida attach 均超时，另一次因存在引擎进程被前置
  检查拒绝。没有取得 getter 返回序列，跨平台差异为 UNKNOWN；失败现场独立保留。
  观察仅启动了本轮隔离只读 AVD；不附加身份未确认的进程，结束后已关闭实例。

## 代码及命令

`native_init_gdb.py` 增加按线程的批次作用域，目标批次返回后关闭其子断点；
`native_observation_scopes.py` 对嵌套和不匹配退出显式报错。Android 脚本增加批次
内 getter 观察，**本轮未成功安装并验证该新 hook**。单元测试验证作用域状态机，
不代替真实引擎完整覆盖。

Linux 在已有隔离 Ubuntu ARM64 guest 的全新私有目录运行，`$...` 是私有输入路径，
目标清单按实际 libg SHA 绑定批次入口及两个候选 getter：

```sh
python3 provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$OUTPUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / --search-root "$UPDATE" \
  --factory-mode register --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" --asset-backend "$BACKEND" \
  --asset-backend-sha256 "$BACKEND_SHA" --timeout 120 --catalog-mode loader \
  --inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue \
  --observe-targets "$TARGETS" --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# scoped wrapper exit 0, native/GDB exit 0, strict status BLOCKED
python tools/pure_linux/run_android_catalog_start_trace.py --adb "$ADB" --engine "$MATCHED_LIBG" \
  --script tools/pure_linux/android_catalog_start_trace.js --output "$ANDROID_OUT"
# two attach timeout attempts exit 1; one pre-existing-process check exit 1
node --check tools/pure_linux/android_catalog_start_trace.js
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1008 passed, 1 skipped
```

Linux 观测 JSON SHA `934b9e49f3bfee2626e4389cba73b0251776fb1e3f527084c5a979c852d82873`；
Linux 结果 SHA `43749c059a0392173a685f581c6a96f81cf56ee7dcb17be67d799b53474447e7`。
首次 Linux 超时结果 SHA `34ddb00b0df04e5a1d1deb591b3b8da3310518aabf1ec8cfaac0cd5f0bef70ec`。
两次 Android 附加失败结果 SHA 分别为
`1e0d27c67bcf5711eac8de82a3e88c57ef2cf14cbc3e1cdf3f04b9194f2620fe` 和
`bf0d4819280a2ab6a3169029983593c3b65bc463fd21978e80d0d100f6f7230a`。
真实 libg SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
私有原始返回值、路径、进程日志及游戏文件不发布。

## 尚未解决

Android 实例需要可靠地在首个相关调用之前附加，并验证 hook 覆盖、线程和输入身份；
之后才能逐批比较结果和追查行创建差异。Android update 的990→908变化、资源完整
语义、配置异常和 Backup 等价均未闭合。ResourceManager、GameState、Step、完整
对局与性能验收仍 NOT_RUN。

[机器证据](pure-linux-catalog-scoped-entry-evidence-20261029.json)。
