# Codex 回执：loading 观察器作用域与偏好文件映射

日期：2026-10-28。分支：`codex/pure-linux-libg-headless-20261026`。
基线：`92d211e406d84234586786817a9600b63b08dd22`。
本阶段提交SHA由该文件所属提交记录，避免在提交中嵌入自指SHA；发布后另核对远端。

## 已完成

| 项目 | 状态 | 实际结果 |
|---|---|---|
| 三组偏好文件映射 | `RUNTIME_OBSERVED` | c→localPrefs，o→storage，p→storage_new |
| 旧观察器漏记复现 | `UNIT_PASS` | 旧代码嵌套fixture预期exit1，c.a.n进入0而非1 |
| 嵌套作用域、异常及异步对象关联修复 | `IMPLEMENTED` / `UNIT_PASS` | 8个JS mock场景通过，真实异步写盘未运行 |
| 受控事务恢复与默认只读 | `IMPLEMENTED` / `UNIT_PASS` | 10个恢复/拒收用例通过 |
| 修复后原版只读链路 | `RUNTIME_OBSERVED` | 两轮各50条记录；最终getKeyValue/j/contains进入1/2/2次，exit0 |
| 全量pure_linux专项 | `UNIT_PASS` | 549 passed，1 skipped，exit0，6.31s |
| 主工程回归 | `UNIT_PASS` | 211 passed，exit0，22.45s |
| 公开回归 | `UNIT_PASS` | 隔离副本1104 passed、42 skipped，exit0，171.98s |
| 凭据及历史证据检查 | 通过 | gitleaks exit0；248份历史XML保持原字节 |

## 证据纠错

旧观察器没有给匹配的store入口建立作用域，内部编码键及无参commit可能被过滤。
历史事务的commit零计数改释为`UNCOVERED_BY_SCOPE_FILTER`，不再用于推断没有落盘。
两个恢复后文件哈希相同只证明末态相同，不排除中间写入。原报告与私有日志保持原字节。

实际命令、输出/工具SHA、50条只读事件摘要见
[阶段报告](PURE_LINUX_LOADING_STORE_PATHS_2026-10-28.md)与
[机器证据](pure-linux-loading-store-paths-evidence-20261028.json)。
原始日志、XML及私有运行内容不进入公开仓库。
发布清单首次因CHANGELOG更新而exit1，修正公开字节登记并保留来源身份；首次失败记录在机器证据中。
最终清单纳入本阶段十份源码与证据，检查2487个文件exit0；没有覆盖历史XML。

## 未完成与下一步

严格`CATALOG_READY_PASS`仍`BLOCKED`。修复后的真实store、磁盘提交、Backup journal及
跨进程恢复未闭合；ResourceManager、GameState、真正Step、no-VM、Android-free、10×及训练
均`NOT_RUN`。本轮没有重复真实store，没有重启或清除AVD数据。

前一次单次store/restore许可已使用。下一次实验的可审查入口已完成：仅对loading键调用一次
目标store并恢复捕获的原值，观察编码键→editor→commit→writeToFile及Backup链；运行前需
新的明确许可。测试通过不替代这一真实平台实验，也不自动放开catalog门。
