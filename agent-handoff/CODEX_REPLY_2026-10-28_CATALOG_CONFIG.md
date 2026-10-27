# Codex 回执：catalog/config 限定推进（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。
源基线：`4b581dd0703f938a86a5ab88aa8722c450858f96`。
提交SHA：包含本回执的提交，可通过 `git log -1 --format=%H -- agent-handoff/CODEX_REPLY_2026-10-28_CATALOG_CONFIG.md` 查询。

| 项目 | 状态 | 实际结果 |
|---|---|---|
| 单键配置候选、字符串/表/资源trace诊断 | IMPLEMENTED | 版本入口守门，未知键拒绝，原libg文件不变 |
| pure_linux专项 | UNIT_PASS | exit0，157通过/1平台条件跳过；行序补充后复跑一致 |
| 主工程/公开回归 | UNIT_PASS | 两入口exit0，211通过/9组XML无失败错误，范围不累加 |
| 真实资源读/seek/EOF/close | PASS（限定） | v70–72 wrapper0/child0，实际APK/update各自字节SHA通过 |
| 自然catalog加载 | RUNTIME_OBSERVED | v78/v79 wrapper0/child0，53steps，ready0→1，162表/24,353行 |
| 真实fallback路径关联 | RUNTIME_OBSERVED | 247次update缺失后APK打开，131次update存在后打开；两次一致 |
| 配置跨进程恢复 | PASS（native候选限定） | 五次exit0及一次预期fresh拒绝exit3，六项均符合预期 |
| CATALOG_READY_PASS | BLOCKED | 加密/commit/BackupManager外部语义未闭合 |
| ResourceManager整体门 | BLOCKED | 仅有局部对象和加载观察 |
| GameState/empty/reset/destroy/Step/动作/完整局/Android对照 | NOT_RUN | 前置门保持 |
| no-JavaVM cold start / Android-free /10×/训练 | NOT_RUN | 本轮未取得这些结论 |

旧故障归因纠正为配置写入桥→JNIEnv→空JavaVM；不是已证明的日志桥。
三份旧报告原字节保留。失败和UNKNOWN均在新报告/JSON中单列。

命令、环境、输入/输出SHA、实际日志SHA及各例退出码见
[报告](PURE_LINUX_CATALOG_CONFIG_2026-10-28.md)与
[机器证据](pure-linux-catalog-config-evidence-20261028.json)。
原始日志、反汇编、DEX、资源、APK、模型、地址及凭据未进入公开提交。
main/origin/main保持 `0012201830cfbfcbfeb3d9dccd91a855028da869`，冻结任务保持原状。

下一步：完成实际观测单键的配置语义闭合，再验manager，随后进入GameState。
当前是契约证据缺口，不是BLOCKED_MISSING_INPUT。
