# Codex 回执：对齐 update 后的 catalog 布局

- `RUNTIME_OBSERVED_LIMITED`：两侧均使用相同 908 项 update；Android 25,810 行，
  Linux 24,353 行，差异仍为 17 槽、1,457 行。
- `RUNTIME_OBSERVED_LIMITED`：Linux 990 项与 908 项运行的布局及已覆盖的 2,078 个
  行字段一致；不同平台的其余状态和读取语义未证明一致。
- `UNIT_PASS`：专项 `1019 passed, 1 skipped`。
- `BLOCKED`：严格 catalog、配置异常及 Backup 等价；战斗生命周期 `NOT_RUN`。

命令、哈希、失败记录和下一步见[报告](PURE_LINUX_MATCHED_UPDATE_LAYOUT_2026-10-29.md)
及[机器证据](pure-linux-matched-update-layout-evidence-20261029.json)。
