# Codex回执：E2.1-F

完成真实PN原桥优先级/冲突/回填矩阵与独立候选差分；G-E2.1严格门保持BLOCKED。
起点`ccac50ec172d7f4626fa956cf75c6da594a55786`；阶段SHA以本文件对应提交及最终消息为准。
仅发布任务分支`codex/headless-engine-plan-20261025`；main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与退出码

`$PY`为已有项目Python，`$RUN`为本轮独占私有目录，`$OLD_RUN`为历史严格门附件目录，
`$CC`为已有NDK的aarch64-linux-android30-clang。占位符仅用于公开路径脱敏；
实际argv、退出码、stdout/stderr和失败日志已本地留存，各次输出独占创建。

```sh
$CC -std=c11 -shared -fPIC -O2 -Wall -Wextra -Werror "$RUN/verification-sources/e2_1_pn_store.c" -o "$RUN/artifacts/libe21pn-rebuilt.so"
$PY tools/run_e2_observed.py --config "$RUN/baseline.config.json" --output "$RUN/baseline"
$PY tools/run_e2_observed.py --config "$RUN/matrix-1.config.json" --output "$RUN/matrix-1"
$PY tools/run_e2_observed.py --config "$RUN/matrix-2.config.json" --output "$RUN/matrix-2"
$PY tools/run_e2_observed.py --config "$RUN/baseline-current.config.json" --output "$RUN/baseline-current"
$PY tools/verify_e2_1f_pn_matrix.py --manifest "$RUN/e2-1f-private-manifest-v1.json" --output "$RUN/bounded-summary-v1.json"
$PY tools/verify_e2_1f_pn_matrix.py --manifest "$RUN/e2-1f-private-manifest-v2.json" --output "$RUN/bounded-summary-v2.json"
$PY tools/verify_e2_1f_pn_matrix.py --manifest "$RUN/e2-1f-private-manifest-v3.json" --output "$RUN/bounded-summary-v3.json"
E2_1F_PRIVATE_MANIFEST="$RUN/e2-1f-private-manifest-v3.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2_1f_pn_matrix.py tools/tests/test_e2_1_observer_release.py tools/tests/test_e2_1_pn_store.py
E2_1F_PRIVATE_MANIFEST="$RUN/e2-1f-private-manifest-v3.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_return_observer.js
node --check tools/e2_1_pn_matrix_candidate.js
$PY tools/run_main_engine_checks.py
$PY tools/run_public_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

NDK复编exit0且SHA与E2.1-E一致。Java observer构建/推送/身份核验十条命令exit0。
四次原生运行exit0；前两次限定验证exit1，分别为换行摘要规则和旧baseline的runner身份。
首次专项exit1（1 failed/49 passed），同为旧runner身份拒收；日志与源文件版本均保留。
最终runner补跑baseline-current后，v3限定验证exit0；严格门仍exit2，不放宽条件。

## 实际测试结果

- 专项：50 passed / 3.34s，含真实附件正向、30类矩阵突变与附件边界负向测试。
- E2全组：184 passed / 102 skipped / 4.85s；未配置历史私有附件的测试显式skip。
- 主工程：211 passed / 31.52s。
- 公开主组：794 passed / 27 skipped / 57.80s。
- 公开八组：affordability 23；selection-timeout 82；position-guards 15 / 3 skipped；
  live-command 25 / 4 skipped；dispatch-command 31 / 3 skipped；reservation-mapping 31 / 2 skipped；
  command-coordinates 38 / 3 skipped；collection-coordinator 65，全部exit0。

测试集合重叠，不累加；skip不计作原生通过。公开回归产生的XML已私有归档，
恢复历史报告原字节。两个JS语法检查exit0。源码清单2491文件校验exit0；
gitleaks凭据扫描exit0；git diff --check退出0。提交前暂存检查及远端SHA以最终回执为准。

## 原生证据与未解决依赖

最终runner的baseline-current和两次matrix各3602份原生快照、9120440bytes、28动作、
tick3601终局以及snapshot/restore/reset回执一致；实际平台资源事件0/失败0/live0。
原始输入和990个update文件前后身份保持。矩阵均在原版完整轨迹之后执行。
每次11项真实原桥矩阵（7读/4写），候选逻辑值/close后磁盘/receipt一致，
原PN raw条目恢复及自有观测引用退休通过。14708附件绑定最终代码，公开仅白名单摘要。

首个缺口：原桥集成与原Cipher/XML存储交接；backup交付、并发等价、崩溃持久化、
未覆盖编码/null输入以及46键可移除性仍unknown。候选未接管战斗桥，未进入E2.2/no-VM。
独占实例已核验身份后释放，专用端口无listener，历史adb身份保持；保留失败证据。
无Android-free、训练准入或10×通过声明。下一步先明确存储交接与backup契约。
