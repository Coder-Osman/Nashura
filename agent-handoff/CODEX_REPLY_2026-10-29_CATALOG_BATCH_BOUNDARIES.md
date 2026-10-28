# Codex 回执：catalog 行数差异批次边界

- `RUNTIME_OBSERVED_LIMITED`：17个差异槽在前4批及第5批入口计数一致；第5批返回
  时首次出现差异，为 Android 3,367、Linux 1,950。
- `RUNTIME_OBSERVED_LIMITED`：第5批返回至最终快照分别增加168和128行。
- `UNIT_PASS`：专项 `1022 passed, 1 skipped`；强化审计后针对性测试2通过。
- `BLOCKED`：严格 catalog、配置和 Backup 等价；战斗生命周期仍 NOT_RUN。

具体调用及因果未知。命令、哈希和覆盖边界见[报告](PURE_LINUX_CATALOG_BATCH_BOUNDARIES_2026-10-29.md)
及[机器证据](pure-linux-catalog-batch-boundaries-evidence-20261029.json)。
