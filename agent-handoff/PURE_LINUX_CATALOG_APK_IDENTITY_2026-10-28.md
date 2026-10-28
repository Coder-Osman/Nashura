# E2.1-N：历史 Android 与 Linux loader 的 APK 资源身份

基线 `e4bc544e9b380eb0762a2b2fe584e8afd78fd02b`；工作分支
`codex/pure-linux-libg-headless-20261026`。本阶段为 **PASS_LIMITED**：
证明两份 APK 的资源字节一致，并关联既有 Android 资源对照与真实 Linux loader
的暂存资源清单；不证明 update、完整 catalog 或战斗状态等价。

## 实际结果

历史 E1 专用 AVD 副本以 `-read-only -no-snapshot-save`、独立端口启动，未启动
游戏进程。只读核验包内 APK SHA 后复制到宿主私有目录；已关闭该实例。
历史 APK SHA `f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b`，
用户提供 APK SHA `a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`；
整包不同，保留各自身份。

逐项解压并计算 SHA-256：两份 APK 各 9,829 个 asset，同名集合一致，字节差异0。
既有 E1 的 Android AssetManager 对照摘要记录9,829项、849,482,445 bytes、
failures=0，其摘要SHA与既有 E1 semantics 附件绑定。历史 Android 运行和 Linux
运行发生在不同时间、不同进程。

既有真实 Linux libg loader 的前后资源清单包含619个暂存 APK asset；其 SHA 全部
与当前 APK 对应资源一致。该 loader 的 backend 日志记录247次打开、247个不同
asset 路径，均属于上述清单。该项绑定的是读取前后身份与打开路径，不能仅凭打开
证明 CPU 解析或消费了完整内容。

仍未证明990个 update 文件与历史 Android 逐项一致、逐请求 fallback 的因果，
也未比较 catalog 内解析后的表值。`CATALOG_READY_PASS=false`；Backup 服务、
ResourceManager、GameState、Step、完整对局和10×均未验收。

## 可复核身份与命令

- loader result SHA `caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014`。
- loader stderr SHA `d17169692bc04fada60abc7fe686c1ac4abbdcd58589e89ceaa7edf22d45b875`。
- Android E1 comparison SHA `5dd3bb7148c7bcc7871fd7e3a7c487a8313129e5ccf6e6b2f6eedb0ba09d2f2c`。
- 本阶段 comparison SHA `6da26d2479953dcf76d3eb8cabc7360917bc2f496d33dfb0cbc82d226f6c5412`。
- 比较工具 SHA `624568f39777c634b22fd8d9f4ece4006daf4ba229e5d081d8025e674e5a8b14`。

APK、本机原始轨迹和资源名称留在私有目录；公开只保留代码、汇总及哈希。
以下变量表示实际命令使用的私有路径：

```sh
python3 tools/pure_linux/compare_catalog_apk_assets.py \
  --stock-apk "$HISTORICAL_APK" --stock-apk-sha256 f1ee6d6146efc8c998843958ae0788e5121c265445c0c7ff5ffa2a5202d7dd9b \
  --current-apk "$USER_APK" --current-apk-sha256 a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f \
  --loader-result "$RESULT" --loader-result-sha256 caad1b680c3a52efb00d49ed8e7762d77e474f5600df788e8ac684006d093014 \
  --loader-stderr "$STDERR" --loader-stderr-sha256 d17169692bc04fada60abc7fe686c1ac4abbdcd58589e89ceaa7edf22d45b875 \
  --e1-comparison "$E1_COMPARISON" --e1-comparison-sha256 5dd3bb7148c7bcc7871fd7e3a7c487a8313129e5ccf6e6b2f6eedb0ba09d2f2c \
  --output "$EXCLUSIVE_OUTPUT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0; 992 passed, 1 skipped
```

公开清单校验 2,614 项、gitleaks 和 `git diff --check` 均 exit0。工具测试包括
重复路径、资源差异、打开路径未绑定、输入与日志身份篡改；测试 fixture 为合成数据。

历史模拟器副本只为读取已安装 APK 启动；本阶段没有执行配置 store、Backup 或
游戏运行，也没有重跑 Linux 引擎。下一步验证实际 update 资源身份和 catalog 表值，
同时继续闭合 Backup 副作用；严格门通过后才进入战斗对象图。
