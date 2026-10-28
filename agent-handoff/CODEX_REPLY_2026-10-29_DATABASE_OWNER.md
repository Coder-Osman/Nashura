# Codex 回执：初始化对象时间边界

`RUNTIME_OBSERVED_LIMITED`：数据库初始化时 root/context 均为空，因此此前对它们
的地址比较不足以说明对象归属；后续 catalog 阶段两者非空，保存的对象地址均
不相等。对象分配、生命周期和所有权仍未知。版本绑定 ELF 重定位关联对象虚表
与上游启动函数，但静态关系不代替动态生命周期证据。

`UNIT_PASS`：专项测试 exit 0，1,073 passed、1 skipped；JavaScript 检查 exit 0。
严格 catalog 门仍 `BLOCKED`，战斗对象图和完整对局 `NOT_RUN`。
详见[报告](PURE_LINUX_DATABASE_OWNER_2026-10-29.md)与
[机器证据](pure-linux-database-owner-evidence-20261029.json)。基线提交为
`cf3da20c41b760e3b985535efcf0ffd04e23836c`，发布提交由 Git 历史标识。
