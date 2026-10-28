# Codex 回执：Backup 自然分支观察与 journal/scheduler 契约

工作基线：`f756ba02eed7d684399d4e4bcc2084e642bf5514`；交付SHA以包含本回执的commit为准。
仅提交并推送 `codex/pure-linux-libg-headless-20261026`，main及冻结部署保持原样。

- `IMPLEMENTED`：六hook被动分支探针、身份/SHA绑定的独占输出runner，以及六方法精确DEX人工审阅绑定工具。
- `NOT_OBSERVED`：真实隔离AVD观察45.006588917秒，目标自然入口0；丢事件0、观察错误0、活动scope0；正常detach。零事件不证明依赖不存在。
- `STATIC_FOUND`：六方法158条指令；journal服务吞IOException后清引用并正常返回，schedule按user去重且忽略JobScheduler整数结果。返回不证明持久化、接受或交付。
- `UNIT_PASS`：新增43通过；隔离全量专项645通过/1跳过、主工程211通过、公开九组1104通过/42跳过，各入口exit0。集合有重叠，不累加。
- 六个新增源码/测试与回归副本逐字节相同，250份原工作树历史XML未变。测试副本独立保存新XML。
- `BLOCKED`：真实Backup副作用及Linux绑定、完整配置异常域仍未闭合；严格catalog保持false。
- `NOT_RUN`：本轮真实libg重载、完整引擎/资源身份复核、ResourceManager、GameState、Step、no-VM、训练与10×。

用户批准的受控store/恢复和主动Backup在此前已完成；本阶段没有重复写入、主动请求、清pending或重启。
历史put分支仍未知。下一项沿File/RandomAccessFile/JobScheduler审阅传递契约，形成有证据支持的最小Linux绑定后，才重跑自然catalog。

实际命令、退出码、环境、输入/输出SHA与私有日志路径见
[报告](PURE_LINUX_BACKUP_BRANCH_OBSERVATION_2026-10-28.md)和
[机器证据](pure-linux-backup-branch-observation-evidence-20261028.json)。
发布清单、凭据扫描与Git差异检查在提交前单独执行；结果以交付提交及对应本机日志为准。
