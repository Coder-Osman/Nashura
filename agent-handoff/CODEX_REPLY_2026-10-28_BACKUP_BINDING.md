# Codex 回执：原生 Backup 候选与自然 loader

基线`13dfbe95ccf927ef710502f5b3a638f910c3e9a7`；交付SHA为本回执所在commit。
分支`codex/pure-linux-libg-headless-20261026`；main及冻结部署保持原样。

- **IMPLEMENTED**：显式原生候选入队、独立双槽归档消费者、严格解析与证据审计。
- **RUNTIME_OBSERVED_LIMITED**：真实ARM64 libg默认对照退出96，两个候选子进程退出0；
  每次自然loader53步，ready 0→1，162张表/24,353行；正常析构、asset finish、dlclose。
- **RUNTIME_OBSERVED_LIMITED**：各两槽1086bytes，独立消费者退出0，归档逐字节验证后清pending。
  原始输入不变，写入仅在新目录副本，未追加Android游戏store。
- **UNIT_PASS**：新增43项；隔离专项900通过/1跳过、主工程211通过、公开1104通过/42跳过；均exit0。
- **BLOCKED**：Android Backup/异常等价性、当前runtime材料身份；三个严格wrapper均exit1，
  `configuration_contract_pass=false`、`catalog_ready_pass=false`。候选不是平台Backup等价实现。
- **NOT_RUN**：ResourceManager、GameState、Step、完整局、严格no-JavaVM及10×。
- 权限拒收批次和首次编译失败均保留；本轮没有启动AVD或更改VM配置。

实际命令、输入/输出与日志SHA见[机器证据](pure-linux-native-backup-binding-evidence-20261028.json)，
详细边界和下一步见[报告](PURE_LINUX_NATIVE_BACKUP_BINDING_2026-10-28.md)。
