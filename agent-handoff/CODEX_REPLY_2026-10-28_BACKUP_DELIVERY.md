# Codex 回执：Backup JobService真实回调

工作基线：`a43620d533e433127057d947347e2238931813e7`；交付SHA为承载本回执的commit。
发布分支：`codex/pure-linux-libg-headless-20261026`；main及冻结任务保持原样。

- **IMPLEMENTED**：四hook观察器、SHA/实例身份绑定runner、严格离线审计与48项工具测试。
- **RUNTIME_OBSERVED_LIMITED**：自有应用一次请求、一次受控强制执行真实job，
  onStartJob→backupNow→cancel原方法返回；10事件、实验6，零drop/error/active。
- **UNKNOWN**：工作线程只有日志覆盖，本次0bytes；非空transport、journal删除、
  scheduler失败、clear直接调用与跨线程强因果未证明。
- 首轮11hook准备期system_server SIGSEGV、exit1、目标请求0；原字节保留，原因UNKNOWN。
- **STATIC_FOUND**：四个真实DEX方法SHA绑定，空文件分支在连接transport前返回0；
  不把静态分支当动态计数。
- **UNIT_PASS**：新增48通过；独立副本专项773通过/1跳过、主工程211通过、
  公开九组1104通过/42跳过，三组均exit0，集合不累加；251份历史XML逐SHA未变。
- **发布检查**：2539项源码清单核验、gitleaks凭据扫描、git diff检查均exit0。
- 专用5582在身份核验后关闭；5580、VM及冻结部署保留，本轮未重复游戏store。
- **BLOCKED**：Linux Backup binding与严格CATALOG_READY_PASS。
- **NOT_RUN**：真实libg重跑、ResourceManager、GameState、GameStateStep、完整局、训练和10×。

实际命令/退出码、输入输出SHA、私有日志定位和下一步见
[报告](PURE_LINUX_BACKUP_DELIVERY_2026-10-28.md)与
[机器证据](pure-linux-backup-delivery-evidence-20261028.json)。
下一项是工作线程完成/清理与失败路径的最小验证，随后才讨论Linux契约接入；
不以JobService返回或工具测试替代原生初始化通过。
