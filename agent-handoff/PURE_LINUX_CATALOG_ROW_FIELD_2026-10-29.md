# E2.1-N：差异表槽对象字段对照

基线 `451d00e`；本阶段为 **RUNTIME_OBSERVED_LIMITED**，严格 catalog 门仍关闭。
在真实 Android 及 ARM64 Linux/Bionic 独立进程的17个差异槽中，对每个行对象读取
偏移 `0x40` 的32位字段，比较对应槽的多重集合，不公开原始字段值。

| 项目 | Android | Linux |
|---|---:|---:|
| 17槽行数 | 3,535 | 2,078 |
| 对方缺少的字段值 | 1,457 | 0 |
| 槽内重复字段值 | 0 | 0 |

每个槽 Linux 的字段值集合均包含于 Android 对应槽。已有版本绑定探针对**部分**
对象将 `0x40` 解释为 global ID，不能将该语义扩展为全部17类；字段相等也不能
证明对象的其他内容相同。该结果提示 Linux 少了对应行，不能确定根因。

Android 样本 `status=FAILED`：其 update 清单从990变908，按原严格门拒收；采集的
两次布局及字段数组一致，初始APK和libg SHA匹配。Linux 的新子进程 `native exit=0`、
wrapper exit1，162槽/24,353行与历史布局一致；独立配置和 Backup 等价仍未通过。
两侧不是同一进程或同一时点，update 删除原因尚未定位；该对照不是有效完整 catalog
验收，不证明无VM冷启动、完整对局或性能。实验结束后隔离Android实例已关闭。

## 身份及实际命令

- libg SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- Linux probe SHA `43799ebf155dbeef6db9ee5fce1127f31917d0fff22a8ac024f175136fed2675`。
- Linux result SHA `923b6963913e5900a140350eacbde83bb5550656e52e3922150b1df76c8ba7d1`，stdout SHA `2355a71752bd13facf1c98ba924dd0fcec6a41bcd25d2e67e645e9002c6e9fe6`。
- Android result SHA `d139c73d0f3bee4caa11b54dd8e6519d98343f52b86f36960c9eae8d91e35bc5`。
- 独立比较摘要 SHA `ca3fa17a0076b2add170fcf95a69148a6fc7c41e9b602aadda4f158788f0d021`。

Linux 使用现有 Ubuntu aarch64 guest 和身份绑定的 payload、619项 APK 暂存及990项
update，NDK27 API30 编译 probe exit0。下列变量代表私有路径；配置参数沿用
`PURE_LINUX_NONEMPTY_PREFERENCE_RECHECK_2026-10-28.md` 的真实身份绑定：

```sh
python3 provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$LINUX_OUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / --search-root "$UPDATE" \
  --factory-mode register --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" --asset-backend "$BACKEND" \
  --asset-backend-sha256 "$BACKEND_SHA" --timeout 55 --resource-trace --catalog-mode loader \
  --inspect-parsed-tables --inspect-row-ids --expected-preload-requests 377 --native-backup-queue \
  --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA"
# wrapper exit 1; native exit 0; strict gate remains closed
python tools/pure_linux/run_android_catalog_layout.py --adb "$ADB" --engine "$LIBG" \
  --script tools/pure_linux/android_catalog_layout.js --output "$ANDROID_OUT" \
  --serial emulator-5630 --frida-address 127.0.0.1:27066 --control-port 28996 \
  --update-reference "$HISTORICAL_UPDATE" --update-reference-sha256 411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942 \
  --recover-spawn-timeout --inspect-row-ids
# exit 1: update inventory changed; captured observations retained
python tools/pure_linux/compare_catalog_row_fields.py --android "$ANDROID_OUT/result.json" \
  --android-sha256 d139c73d0f3bee4caa11b54dd8e6519d98343f52b86f36960c9eae8d91e35bc5 \
  --linux-result "$LINUX_OUT/result.json" --linux-result-sha256 923b6963913e5900a140350eacbde83bb5550656e52e3922150b1df76c8ba7d1 \
  --linux-stdout "$LINUX_OUT/stdout.log" --linux-stdout-sha256 2355a71752bd13facf1c98ba924dd0fcec6a41bcd25d2e67e645e9002c6e9fe6 \
  --output "$PRIVATE_COMPARISON"
# exit 0: bounded analysis only
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1002 passed, 1 skipped
```

Linux前两次采集各以 `-25` 退出，因逐行内存范围检查导致 strace 日志达到32MiB
上限；两份失败结果 SHA 分别 `552dadcaa425957271a1fad112148c5da25eee21564ce265b92fb83b6511b26f`、
`3f23daf40bc22eff07ae82e93e6711b7fc1e91be76e5c004567176ab323da02f`。
第三次仅记录17槽并限制跟踪范围后 native exit0；保留失败记录，不推断早期失败
是引擎语义故障。

下一步追踪差异字段所对应行的创建/筛选分支，核验 Android update 变化时点及实际
加载参数；严格 catalog 通过前不推进 ResourceManager、GameState 或真实 Step。
[机器证据](pure-linux-catalog-row-field-evidence-20261029.json)。
