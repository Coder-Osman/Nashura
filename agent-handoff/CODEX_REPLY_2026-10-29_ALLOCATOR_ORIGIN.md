# Codex 回执：分配器来源

- `RUNTIME_OBSERVED_LIMITED`：真实目标分配器以 `AndroidMainAllocator` 构造，再跨线程用于持有者工厂。
- `UNIT_PASS`：身份审计 exit 0；相关测试 1085 passed、1 skipped。
- `BLOCKED`：工厂第二参数与释放语义、配置异常和 Backup 等价；严格 catalog 门未通过。
- `NOT_RUN`：Linux 工厂、ResourceManager、GameState、Step、完整对局和性能验收。

命令与边界见[报告](PURE_LINUX_ALLOCATOR_ORIGIN_2026-10-29.md)和[证据](pure-linux-allocator-origin-evidence-20261029.json)。提交身份以 Git 历史为准。
