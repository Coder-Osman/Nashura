# Codex 回执：catalog 批次入口观测

- `RUNTIME_OBSERVED_LIMITED`：Linux 真实 libg 记录 5 次批量调用、158 次类型候选和
  155 次 ID 候选调用，返回记录完整；作用域及真实进程正常结束。
- `UNIT_PASS`：线程作用域状态机覆盖并发线程、嵌套及缺失退出。
- 专项测试：`1008 passed, 1 skipped`；Android 脚本 `node --check` exit 0。
- `BLOCKED`：Android 两次 attach 超时；新 hook 未实测，跨平台对照为 UNKNOWN。
- `BLOCKED`：严格 `CATALOG_READY_PASS`、完整配置与 Backup 等价。
- `NOT_RUN`：ResourceManager、GameState、Step、完整对局及 10 倍吞吐。

已授权的一次 Android 配置 store/restore 在此前阶段执行并记录；本轮未重复写入。
本轮隔离 AVD 已关闭，私有日志未发布。详细命令、退出码、SHA、失败和下一步见
[阶段报告](PURE_LINUX_CATALOG_SCOPED_ENTRY_TRACE_2026-10-29.md)及
[机器证据](pure-linux-catalog-scoped-entry-evidence-20261029.json)。
