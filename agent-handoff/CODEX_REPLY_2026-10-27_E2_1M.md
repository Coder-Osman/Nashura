# Codex 回执：E2.1-M 自然 PN 持久替换

基线 `abe4fc1451e10682654beeaf1ba67b865131830b`；阶段 SHA 为本文件所在提交。
仅发布 `codex/headless-engine-plan-20261025`，不修改 main 或冻结任务。

## 实际命令与结果

`$PY` 为既有项目 Python，`$RUN` 为本轮独占私有目录。实际 argv、配置和原始日志
留本机，以下使用脱敏路径变量。

```sh
$PY tools/run_e2_observed.py --config "$RUN/<run>.config.json" --output "$RUN/<run>"
# 十个成功 run，各 exit 0；五组原版/替换，每 run 含异常前后两完整生命周期。
# 五原版首尝试审计路径冲突 exit 1；一替换并发重入 abort/exit 1，失败输出全部保留。
$PY "$RUN/reproduce.py"
# exit 0；clang/javac/d8 三条命令 exit 0，SO/DEX 与部署逐字节一致。
$PY tools/verify_e2_1m_persistent_bridge.py --manifest "$RUN/private-manifest.json" --output "$RUN/verified-m-1.json"
# exit 0，73183 附件，bounded_replacement_verified，strict_gate=BLOCKED。
E2_1M_PRIVATE_MANIFEST="$RUN/private-manifest.json" $PY -m pytest -q tools/tests/test_e2_1m_persistent_bridge.py tools/tests/test_e2_1m_native_backend.py tools/tests/test_e2_1m_post_edge.py --junitxml="$RUN/tests-m-1.xml"
# exit 0，48 passed / 11.54s。
E2_1M_PRIVATE_MANIFEST="$RUN/private-manifest.json" $PY -m pytest -q tools/tests -k e2 --junitxml="$RUN/tests-e2-1.xml"
# exit 0，356 passed / 350 skipped / 149 deselected / 15.66s。
PATH="$JDK/bin:$PATH" $PY -m pytest -q tools/tests/test_e2_1_observer_release.py --junitxml="$RUN/tests-java-1.xml"
# exit 0，1 passed / 0.61s；JDK 为已有安装，不安装系统组件。
git diff --check
# exit 0。
```

二十轨迹各 3602 快照、28 动作、9120440 bytes、tick3601 终局逐字节一致，
前后各17非status控制回执一致。异常矩阵后按语义恢复存储、保留解密上下文，再运行
完整局；不是损坏存储不恢复的完整局证明，也不是新增训练种子或吞吐基准。

原输入和990update身份保持，平台资源事件/Provider failures/live assets均0。
自然read/write来源、线程、phase、raw操作与真实commit可追溯；各替换保留133非PN fallback。
Java身份/store/XML/backup/VM仍保留，46键移除0；严格门仍BLOCKED，未重跑整体strict CLI。
并发重入已真实失败，等价性仍unknown；无no-VM、Android-free、训练或10×声明。

## 发布验证

在独立发布副本 `$CHECK` 执行，未覆盖仓库历史 XML。以下检查均 exit 0：

```sh
cd "$CHECK"
$PY tools/check_public_snapshot.py
# 2570 个源码条目通过，0.46s。
$PY tools/run_public_checks.py
# 165.85s；各组实际结果见下表。
$PY tools/run_main_engine_checks.py
# 211 passed / 23.97s；命令总耗时24.59s。
gitleaks dir . --config .gitleaks.toml --redact
# no leaks，2.46s。
```

| 公开回归组 | 实际结果 |
|---|---|
| public | 794 passed, 27 skipped / 51.47s |
| affordability | 23 passed / 4.41s |
| selection-timeout | 82 passed / 81.26s |
| position-guards | 15 passed, 3 skipped / 0.13s |
| live-command | 25 passed, 4 skipped / 0.97s |
| dispatch-command | 31 passed, 3 skipped / 0.95s |
| reservation-mapping | 31 passed, 2 skipped / 0.92s |
| command-coordinates | 38 passed, 3 skipped / 2.12s |
| collection-coordinator | 65 passed / 20.04s |

这些是工程检查；缺少其他私有附件导致的 skip 不计作原生验证，重叠集合不累加。
发布准备辅助脚本首次出现括号语法错误，metadata/manifest/snapshot 各 exit 1；
未刷新的清单检查也 exit 1。修正脚本后按顺序重跑均 exit 0，原失败记录留本机，
SHA256 为 `52f925868fa223f845399be3b5a997f40a86588ee6d0519eaca8158eafe1e422`。
该辅助失败与引擎实验失败分别记录，未改写实验 manifest 或历史 XML。

## 未解决依赖

首个缺口为Mainloop与外部probe并发重入；先观测原线程/锁边界，再设计实现。
commit(false)、Java身份、原store/XML/backup、崩溃恢复仍未闭合。
成功run游戏进程已释放，专用AVD/adb保留供续验。详见阶段报告和失败索引。
