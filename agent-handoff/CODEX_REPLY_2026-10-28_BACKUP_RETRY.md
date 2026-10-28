# Codex 回执：真实 Backup 失败重排与重试

基线`5d1a34679e36207568190a583e316201d51558f6`；交付SHA为本回执所在commit。
分支`codex/pure-linux-libg-headless-20261026`；main及冻结任务保持原样。

- **IMPLEMENTED**：一次性自有失败agent、v2远端退出码采集、73份归档严格审计。
- **RUNTIME_OBSERVED_LIMITED**：一次自有store/request，首次真实IOException后目标重排，
  未追加请求的第二次消费成功；pending `否→是→是→否`，journal目标`0→1→1→0`。
- **RUNTIME_OBSERVED_LIMITED**：成功流259bytes、transport231bytes、XML215bytes、state72bytes；
  XML不变，transport偏移16后与XML一致。非游戏、非Linux原生引擎证据。
- **STATIC_FOUND**：真实DEX三个方法的AgentException/TaskException、重排及收尾分支。
- **UNKNOWN/NOT_RUN**：删除方法计数、transport头、TaskException、自然到期及崩溃恢复。
- **UNIT_PASS**：新增47项，隔离专项857通过/1跳过、主工程211通过、公开1104通过/42跳过；均exit0。
- 253份原XML的SHA保持一致；6份待发布源码与测试副本逐字节一致。
- 保留v1采集缺陷及初次负例失败；v2没有重跑真实store。专用5582已关闭，5580与Ubuntu保留。
- **BLOCKED**：Linux Backup所有权/绑定及严格CATALOG_READY_PASS。
- **NOT_RUN**：本轮Linux libg重载、自然catalog、ResourceManager、GameState、Step和10×。

实际命令、退出码、输入/输出SHA及发布结果见[机器证据](pure-linux-backup-retry-evidence-20261028.json)，
范围与下一步见[报告](PURE_LINUX_BACKUP_RETRY_2026-10-28.md)。
