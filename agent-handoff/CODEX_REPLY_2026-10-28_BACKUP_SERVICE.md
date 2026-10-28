# Codex 回执：E2.1-N Backup 服务真实分支

- 基线：`85ed2498d70aa7d3a3ed1cdfe79027710f45cb9e`。
- 交付 SHA：包含本回执的提交，当前分支 `codex/pure-linux-libg-headless-20261026`。
- **IMPLEMENTED**：自有测试应用、八 hook 观察器、独占输出 runner 与严格离线审计。
- **RUNTIME_OBSERVED_LIMITED**：首次入队写 journal、重复请求不写；
  独立平台 scope 的 JobScheduler 提交返回1，保持独立归因。
- **UNIT_PASS**：新增36项；专项725通过/1跳过、主工程211通过、
  公开1104通过/42跳过，三个最终命令 exit0，集合不累加。
- **BLOCKED**：Linux Backup 副作用绑定，CATALOG_READY_PASS=false。
- **NOT_RUN**：本轮libg、ResourceManager、GameState、真实Step、无VM、完整局及吞吐。

## 实际命令、身份与输出

实际审计命令、11条实际ADB操作、七条构建命令（测试密码脱敏）、
输入/输出SHA与六次尝试见
[机器证据](pure-linux-backup-service-runtime-evidence-20261028.json)。
三组回归实际使用 `/opt/anaconda3/bin/python3` 在独立源码副本执行：
`-m pytest -q -p no:cacheprovider tools/pure_linux`、
`tools/run_main_engine_checks.py`、`tools/run_public_checks.py`。

v6 result SHA `7d1b4ccafb67880d2a9b2130f9c873b356cb92d60f29203e428c787c624c6d15`；
audit SHA `fff35ada5bd774e675cc8a4c607af16c7bebc8edddf7492ff1788819a897f71c`。
24事件、八hook、零drop/error/active；system_server身份前后一致。
journal95bytes、三条完整边界、目标应用精确一条；不声称完整解码域已验证。
专用5582实例已确认归属并关闭；旧游戏实例及VM保持原状。
248份tracked XML未变，测试源码与待提交源码一致。

## 限制、失败与下一步

Job接受不是交付；Binder到handler跨线程关联没有强因果绑定。
自有应用真实平台分支不是历史游戏事务，也不是Linux引擎运行。
v1–v5失败/覆盖不足、旧解释器失败、v1–v4源码归档缺口全部保留；
详见[报告](PURE_LINUX_BACKUP_SERVICE_RUNTIME_2026-10-28.md)。

下一唯一阻塞是Linux Backup服务副作用绑定，继续处理pending/journal归属与
scheduler生命周期/错误/交付契约；`unresolved_backup`仍显式失败。
通过绑定后再运行真实libg自然catalog；严格门前不推进GameState。

## 发布

发布检查：`tools/check_public_snapshot.py` exit0，2531个文件通过；
`gitleaks dir . --config .gitleaks.toml --redact --no-banner` exit0，no leaks；
`git diff --check` exit0。首次清单生成格式错误已修复，保留历史source SHA与变换说明。

fetch40秒超时，远端新鲜度未确认。按当前分支提交并有界推送；
最终提交SHA、发布检查和推送结果见实际Git回执及最终回复。
main与历史证据不覆盖，私有输入与原始trace不发布。
