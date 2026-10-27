# Codex回执：E2.1-D

本阶段完成自然PN_POPUP返回与观测器自有引用退休的限定补验；严格G-E2.1仍BLOCKED。
起点提交 `e58888b4da023f1d26f29935083ae7cbcc653db6`，成果只发布到任务分支。
阶段提交SHA以本文件对应的Git提交及最终消息为准；main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令

以下 `$PY` 为已有项目Python，`$RUN` 为本轮私有独占目录；发布前保留每条命令、
退出码和stdout/stderr。占位符用于脱敏，不表示执行时缺少输入。

```sh
$PY tools/run_e2_observed.py --config "$RUN/baseline.config.json" --output "$RUN/baseline"
$PY tools/run_e2_observed.py --config "$RUN/cold.config.json" --output "$RUN/cold-3"
$PY tools/run_e2_observed.py --config "$RUN/cold.config.json" --output "$RUN/cold-4"
$PY tools/verify_e2_1d_evidence.py --manifest "$RUN/e2-1d-private-manifest-v3.json" --output "$RUN/bounded-verification-3.json"
$PY tools/e2_1d_public_summary.py --manifest "$RUN/e2-1d-private-manifest-v3.json" --output agent-handoff/e2_1d_evidence_summary.json
E2_1D_PRIVATE_MANIFEST="$RUN/e2-1d-private-manifest-v3.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2_1d_evidence.py tools/tests/test_e2_1_cold_observer.py tools/tests/test_e2_1_observer_release.py
E2_1D_PRIVATE_MANIFEST="$RUN/e2-1d-private-manifest-v3.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck-1.json"
$PY tools/check_public_snapshot.py
$PY tools/run_public_checks.py
$PY tools/run_main_engine_checks.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

## 结果

原生baseline与两次成功观测进程均完整3602快照、28动作一致；成功命令均exit0。
两次自然写的两个原store各commit(true)一次，自然读零commit；正常进程重启读回一致。
观测器恢复原引用、释放自有6个Java引用及1个JNI global reference。
D限定验证exit0，25项新专项通过；E2专项146 passed/59 skipped；主工程211 passed。
旧严格门exit2；原桥早装onLeave未知、两次失败观察及诊断错误仍保留。

公开回归exit0：主组794 passed/27 skipped；affordability 23 passed；selection-timeout
82 passed；position-guards 15 passed/3 skipped；live-command 25 passed/4 skipped；
dispatch-command 31 passed/3 skipped；reservation-mapping 31 passed/2 skipped；
command-coordinates 38 passed/3 skipped；collection-coordinator 65 passed。
本轮XML与命令日志归档于私有证据目录，公开仓库历史XML保留原字节。
凭据扫描exit0、no leaks found；源码快照清单2473文件校验exit0；Git whitespace检查exit0。
D限定验证第二次复验exit0，仍使用v3身份与原始附件，未改变严格门条件。
原始附件仅本机保留，公开摘要不能独立重验原生结果。
独占实例已回收，原5037 adb身份保持；未改原始二进制和资源，未进入E2.2/训练/10×。

## 未解决与下一步

PN尚未native替换；持久化与失败等价、46键可移除性、游戏Java对象生命周期未闭合。
下一步先定义PN原生存储状态机并保留真实失败通道，再做保留VM的逐项对照，
不将正常重启读回或自有引用退休外推为无VM运行。
