# Codex 回执：Backup pending 去重分支

工作基线：`76ca520cae256b419eeaad177f3a47ac8a9b32e3`；交付SHA以承载本回执的commit为准。
仅发布 `codex/pure-linux-libg-headless-20261026`，main及冻结任务保持原样。

- `IMPLEMENTED`：目标队列只读探针、独占私有输出runner、精确方法SHA绑定人工审阅契约。
- `STATIC_FOUND`：真实服务端方法40指令；已有pending请求跳过journal调用，正常非null
  targets路径仍schedule；异常路径及交付单独保留未知。
- `RUNTIME_OBSERVED`：两轮各两次未持锁采样，目标pending和participants存在，journal对象存在。
  当前快照不证明历史请求分支；观察线程UID与游戏调用者UID不同，没有主动调用targets制造证据。
- `UNIT_PASS`：新增25通过；隔离专项602通过/1跳过、主工程211通过、公开九组1104通过/42跳过，
  入口均exit0，集合不累加；五个文件与测试副本SHA相同，250份历史XML未变。
- 两次只读失败、metadata检查、SHA手抄错误拒收与首次fixture失败保留，未放宽生产校验。
- 发布检查：源码清单2500项通过，gitleaks exit0且无发现，git diff --check exit0。
- `BLOCKED`：完整loading配置/Backup副作用契约及Linux绑定，严格catalog仍false。
- `NOT_RUN`：真实libg重跑、ResourceManager、GameState、Step、无VM、训练、10×。

本轮没有新增store或Backup请求；用户授权的写入/恢复已经在上阶段结束。
下一项只被动观察自然请求的真实put旧值，并关联journal/schedule；无触发则保持未观测。
严格catalog通过前保持后续对象图关闭。

命令、输入输出SHA、私有日志路径及失败边界见
[报告](PURE_LINUX_BACKUP_QUEUE_SEMANTICS_2026-10-28.md)及
[机器证据](pure-linux-backup-queue-semantics-evidence-20261028.json)。
