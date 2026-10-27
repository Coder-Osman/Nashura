# Codex 回执：用户 APK 足够继续原生加载

任务分支 `codex/pure-linux-libg-headless-20261026`，父提交
`2b67d0d3a9a1ae2a972d6067fff8ea3208024abc`。

| 子项 | 状态与实际结果 |
|---|---|
| 用户输入核验 | IMPLEMENTED：版本一致，8/8 ARM64 库 SHA 一致，9,829 资产逐项 SHA 一致 |
| runtime/update 身份 | 214/214、990/990 匹配，fingerprint 一致 |
| 历史 APK 锁定 | MISSING_INPUT：整包 SHA 仍不同；旧清单和 gate 不变 |
| 新输入独立真实加载 | LOAD_ONLY：真实 ARM64 Linux + Android linker，dlopen/dlclose exit0 |
| 输入完整性 | 187 个暂存输入前后 SHA 相同，无 shim 或原版文件修改 |
| 原生初始化 / 生命周期 | NOT_RUN：无战斗初始化、动作、tick、snapshot/reset/终局 |
| no-JavaVM 冷启动 / Android-free / 10× / 训练 | NOT_RUN |

实际命令、输入与输出 SHA、日志标识、警告及未解决依赖见
[实验报告](PURE_LINUX_USER_APK_LOAD_2026-10-27.md)与
[机器可读证据](pure-linux-user-apk-load-evidence-20261027.json)。

下一项是内容上下文、manager 和 GameState 的独立初始化闭包。加载成功解决了
此前尚未实测的动态装载问题，没有证明战斗逻辑已脱离 Android。

## 发布前工程检查

在 HEAD 的独立源码快照中运行，未覆盖仓库历史 XML；各组分别统计，不相加：

- `python3 -m unittest discover -s tools/pure_linux -p 'test_*.py' -v`：29 通过，exit0。
- `python tools/run_main_engine_checks.py`：211 通过，exit0。
- `python tools/run_public_checks.py`：exit0；主组 794 通过、27 跳过；其后各组分别
  为 23、82、15、25、31、31、38、65 通过，跳过分别为 0、0、3、4、3、2、3、0。

首次快照准备使用的系统 Python 不支持 `tarfile.extractall(filter=...)`，准备失败，
随后两个检查入口因缺文件 exit2；失败日志保留。新隔离快照在逐项检查成员路径及类型后
展开，以上为重跑结果。工程回归和跳过项均不替代原生生命周期证据。
