# E2.1-N：历史 Android 与 Linux loader 的 update 资源身份

基线 `6165b5d64a176842829e1c0d4a73a6546e20c5b9`，工作分支
`codex/pure-linux-libg-headless-20261026`。本阶段为 **PASS_LIMITED**，
复核历史 Android E1 update 文件清单与真实 Linux loader 运行时清单；没有重跑游戏。

## 结果

历史 Android E1 namespace 对照前后 update 清单均为990项，文件相同，归档SHA均为
`411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942`；该SHA
记录在 E1 semantics 附件中。真实 Linux libg loader 的前后清单也为990项。
两组路径集合一致、逐项 SHA 差异0。Linux trace 中实际打开的131次、131个不同
update 路径全部属于该清单，SHA 与 Android 历史清单一致。

载入 trace 的关闭、完整终止、资源清单身份及 backend 计数继续使用既有审计器核验。
这是**文件身份**证据：mmap 不证明读取全部映射字节，也未证明请求级 fallback
因果或 catalog 表的每个字段一致。Android 和 Linux 证据来自不同时间的进程。
严格 `CATALOG_READY_PASS=false`；Backup 服务、ResourceManager、GameState、
Step、完整对局和10×均未验收。

## 命令与身份

变量仅表示私有路径；实际文件内容和原始日志未公开。

```sh
python3 tools/pure_linux/compare_catalog_update_assets.py \
  --android-before "$E1_BEFORE" --android-before-sha256 411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942 \
  --android-after "$E1_AFTER" --android-after-sha256 411aa1c884c90366e4cae2600d13e886aebe000af282847eb494d65d7d62a942 \
  --loader-result "$RESULT" --loader-result-sha256 caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014 \
  --loader-trace "$TRACE" --loader-trace-sha256 0a34bb830c6e805de966479b0cb8276b7a8b6068d76cd440b32c8c6b758ca071 \
  --loader-stderr "$STDERR" --loader-stderr-sha256 d17169692bc04fada60abc7fe686c1ac4abbdcd58589e89ceaa7edf22d45b875 \
  --assets-root "$GUEST_ASSETS" --update-root "$GUEST_UPDATE" --cwd "$GUEST_CWD" \
  --output "$EXCLUSIVE_OUTPUT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0; 999 passed, 1 skipped
```

实际输出SHA `8983b1bf148dc576da1dae7613936bb00174999ee84a48f758032de045456854`；
工具SHA `e44089a55101f8315f12a265a814ea0c99eb2f590d813806908797cf0a3817e8`。
历史E1 `semantics.json` SHA `eecd21e95109b2866fdfeb2696c623968edca3d096ac99825a5398a0ca022e99`。
独立合成测试覆盖错误路径、重复、清单差异和篡改trace拒收；不将合成fixture当作原生运行。

下一步对照真实 Android 与 Linux catalog 的解析表值，同时继续闭合 Backup 和配置
异常域；只有严格 catalog 门通过后才进入后续战斗对象。
