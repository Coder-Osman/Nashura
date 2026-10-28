# Codex 回执：受控 store 与原值恢复复核

分支 `codex/pure-linux-libg-headless-20261026`，基线
`cdc5fae77aa3ed5f1ac4eaeebebb07be8226792d`。

- **RUNTIME_OBSERVED_LIMITED**：真实 AVD 中 `server-login` → 测试值 → `server-login`，
  后续只读复核仍读到原值。
- **RUNTIME_OBSERVED_LIMITED**：目标写入与恢复各经过两个槽；共四次 commit 均 true，
  commit/disk-write 调用链已观察。
- **RUNTIME_OBSERVED_LIMITED**：两次 Backup 请求进入客户端 Binder、服务端
  `dataChangedImpl` 和 `KeyValueBackupJob.schedule`。
- **UNRESOLVED**：目标包在事务前已处于 pending 队列；本窗口没有观察到
  `writeToJournalLocked`，也未捕获四阶段偏好文件字节，所以不能证明 journal 新增/去重
  分支或本次文件逐字节恢复。
- **BLOCKED**：Backup/Linux callback 等价性及严格 `CATALOG_READY_PASS` 仍未闭合。
- **NOT_RUN**：ResourceManager、GameState、真实 Step、完整战斗、no-VM、Android-free、
  10×及训练。
- 首次 Frida attach 失败保留，确认没有写入；AVD 中已有 Frida 16.7.19 启动后只执行
  一次 store/restore。实验 AVD 已关闭，配置身份不变。

验证：`tools/pure_linux` 953 passed/1 skipped；主工程 211 passed；公开回归
1,104 passed/42 skipped；源码清单2,583项通过；gitleaks无发现；均exit 0。
详细身份与事件边界见
[阶段报告](PURE_LINUX_CONTROLLED_STORE_RECHECK_2026-10-28.md)和
[机器摘要](pure-linux-controlled-store-recheck-evidence-20261028.json)。交付提交 SHA
以包含本回执的提交为准。
