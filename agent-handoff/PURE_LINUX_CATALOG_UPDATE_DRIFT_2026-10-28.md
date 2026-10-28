# E2.1-N：Android catalog 对照中的 update 清单变化

基线 `73b1fbc06f19c0c3d4808abcb995d304fa8d28b6`。两次独立只读 AVD 启动
均发现 update 清单在游戏进程启动前后变化，**两次捕获均被严格门禁拒收**。
布局采集、清单核验和独立离线审计使用原始真实数据；没有放开 catalog 门。

## 结果

两次启动前的990项与历史 E1 清单逐项相等；启动后均为同一组908项：
减少82项，新增0项，剩余文件 SHA 改变0项。减少项按文件后缀分为65个 `.sc`
和17个 `.ogg`，与既有 Linux loader 轨迹中的131个 update 打开路径无交集。
后缀和路径交集不证明这些文件的语义、不会影响 catalog 或没有其他读取路径。
删除由哪个进程、在何时发生仍为 UNKNOWN。

第二次失败捕获仍保存已观察的表布局：162槽、25,810行，与先前 Android 布局
逐项一致。对 Linux 的17槽、1,457行差异因此得到重复观察；此次启动时 update
文件从990变908，且变化时刻与加载的因果未捕获，不能宣称输入全程一致或判定根因。

## 实验与核验

`e1-only / emulator-5630` 使用现有 API30 ARM64 AVD、只读及不保存快照；两次
启动各在独立目录保存 before/after 清单。每次核验历史 APK 和 libg SHA；使用
Frida 16.7.19 附加本实验进程，只调用 status 与只读布局快照，没有对局或 store。
两次 runner exit1，原因均为 `update inventory after capture differs`。此前 v3 是
独立历史采集，不补写其 update 身份。实验结束后本轮实例和服务已退出。

新增 `audit_catalog_update_drift.py` SHA绑定两次清单、结果、先前布局和 Linux
trace，核对一致性及路径交集。runner 增加显式清单身份检查，失败仍保存已观察
布局和错误；spawn超时的限定恢复仅用于确认之前不存在目标进程且本轮出现唯一PID。

```sh
python tools/pure_linux/run_android_catalog_layout.py --adb "$ADB" --engine "$MATCHED_LIBG" \
  --script tools/pure_linux/android_catalog_layout.js --output "$PRIVATE_V5" \
  --serial emulator-5630 --frida-address 127.0.0.1:27066 --control-port 28996 \
  --update-reference "$HISTORICAL_UPDATE" \
  --update-reference-sha256 411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942 \
  --recover-spawn-timeout
# v4、v5 exit 1，update identity changed
python tools/pure_linux/audit_catalog_update_drift.py --reference-layout "$V3/result.json" \
  --v4 "$V4" --v5 "$V5" --trace "$LINUX_TRACE" --identities "$PRIVATE_IDENTITIES" \
  --assets-root "$ASSETS_ROOT" --update-root "$UPDATE_ROOT" --cwd "$LINUX_CWD" \
  --output "$PRIVATE_AUDIT"
# exit 0: audit of rejected captures, not a parity gate
python -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0: 1001 passed, 1 skipped
```

审计输出 SHA `054c225d6c0c3c18cf9a4cc21656d57710a43b1d53ab7b93d26ce57d3cc8b931`；
输入、输出身份与结果见[机器证据](pure-linux-catalog-update-drift-evidence-20261028.json)。
原清单、文件路径和原始日志留在私有目录。下一步追踪启动期 update 删除和表槽
创建的实际调用、对齐截获时点，随后再重做比较；Backup配置、catalog严格门与
ResourceManager/GameState/Step仍未验收。
