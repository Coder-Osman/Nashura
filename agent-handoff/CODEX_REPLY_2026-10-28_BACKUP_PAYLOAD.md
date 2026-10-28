# Codex 回执：非空 Backup 与队列消费

基线 `0ad2e5a04fe3abd6eb7e977184418cf5170b4c9d`；交付SHA为承载本回执的commit。
分支 `codex/pure-linux-libg-headless-20261026`；main、冻结任务和历史证据保持原样。

- **IMPLEMENTED**：自有非空payload fixture、独占runner、SHA/命令/文件审计器及37项测试。
- **RUNTIME_OBSERVED_LIMITED**：一次自有store/request、一次非增量backupnow成功；
  目标仍pending。随后一次bmgr run消费队列，目标pending及对应journal消失。
- **RUNTIME_OBSERVED_LIMITED**：transport233bytes、XML217bytes、state72bytes前后一致；
  transport偏移16之后与XML相同，四份存活journal不含目标。
- **UNKNOWN**：transport header语义、journal删除方法计数、工作线程方法计数；
  自然到期、失败/重排/崩溃恢复 **NOT_RUN**。
- **UNIT_PASS**：新增37项通过；独立专项810通过/1跳过、主工程211通过、
  公开九组1104通过/42跳过，均exit0；252份原XML保持原字节。
- **审计**：56份命令归档核验，v4离线exit0；早期构建与审计失败记录保留。
- 专用5582核验身份后关闭；5580与Ubuntu保留，没有重复真实游戏store。
- **BLOCKED**：Linux Backup binding、严格CATALOG_READY_PASS。
- **NOT_RUN**：本轮libg重载、自然catalog、ResourceManager、GameState、Step、完整局及10×。

实际命令/退出码、环境和输出SHA见[机器证据](pure-linux-backup-payload-evidence-20261028.json)，
实验限制和下一步见[报告](PURE_LINUX_BACKUP_PAYLOAD_2026-10-28.md)。
下一项是失败/重排和服务所有权契约，工具测试及正常Android备份不代替Linux原生初始化。
