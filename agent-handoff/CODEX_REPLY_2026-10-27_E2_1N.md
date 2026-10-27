# Codex 回执：E2.1-N 并发诊断与有界串行门

基线 `256eaa6db54bd02539188fd4e6d7d7c42aaca72d`；阶段 SHA 为本文件所在提交。
只发布任务分支，不修改 main、冻结任务、原始引擎/资源，不启动训练或 E2.2。

## 实际命令与退出码

`$PY/$RUN/$JDK` 指既有 Python、独占私有目录和现有 JDK；公开命令使用脱敏变量。
具体 argv、命令回执和原始日志留本机。

```sh
$PY tools/run_e2_observed.py --config "$RUN/<run>.config.json" --output "$RUN/<run>"
# 五个纳入核验的尝试：
# original-control-lock-v2: exit 0 / 84.486s
# original-timeline-race-flush-v4: exit 0 / 84.170s
# replacement-diagnostics-race-lock-v2: exit 124 / 600.015s
# replacement-gate-preload-race-v5: exit 0 / 70.795s
# replacement-gate-repeat-race-v6: exit 0 / 84.367s
# 另两失败：replacement-dispatch-race-lock-v3 exit 1；replacement-gate-race-v4 exit -9。
$PY tools/verify_e2_1n_concurrency.py --manifest "$RUN/private-manifest-n-v7.json" --output "$RUN/verified-n-v7-1.json"
# exit 0 / 3.200s，29164附件、八完整轨迹，bounded_diagnostics_verified。
NASHURA_E21N_MANIFEST="$RUN/private-manifest-n-v7.json" PATH="$JDK/bin:$PATH" $PY -m pytest -q tools/tests/test_e2_1n_evidence.py tools/tests/test_e2_1n_diagnostics.py tools/tests/test_e2_1n_dispatch_gate.py tools/tests/test_e2_1i_cold_pn_bridge.py tools/tests/test_e2_1k_native_backend.py --junitxml="$RUN/tests-n-1.xml"
# exit 0，34 passed / 54 skipped / 3.93s；在新增真实附件损坏负测前运行。
NASHURA_E21N_MANIFEST="$RUN/private-manifest-n-v7.json" PATH="$JDK/bin:$PATH" $PY -m pytest -q tools/tests -k e2 --junitxml="$RUN/tests-e2-n-1.xml"
# exit 0，349 passed / 392 skipped / 149 deselected / 7.63s；含新增四项真实附件负测。
$PY "$RUN/reproduce_n_v1.py"
# exit 0 / 1.521s；clang/javac/d8三命令exit0，SO/DEX与部署字节一致，无guest变更。
```

八轨迹各3602快照/28动作全字节一致，snapshot/restore/reset/终局控制回执一致。
两串行run各154门调用完成、无拒绝/锁错误；原版部分返回覆盖unknown，
无门替换已观测重入拒绝后timeout，原因unknown，后续恢复与最终身份未覆盖。
资源门成功run均通过，原输入/990update身份保持；每替换133非PN fallback。
Java身份/store/XML/backup/VM保留，46键移除0，strict仍BLOCKED，通用并发等价unknown。
工程测试和原生实验分别报告，skip不算原生覆盖，重叠测试集合不累加。

## 发布验证

以下检查在独立公开副本中执行，日志、命令回执和本次 XML 留在独占私有目录，
不覆盖仓库历史 XML。各测试集合分别报告，不累加为原生覆盖。

```sh
$PY tools/check_public_snapshot.py
# exit 0 / 0.371s；2581 个源码文件身份通过。
$PY tools/run_public_checks.py
# exit 0 / 167.299s；本次九组共1104 passed / 42 skipped，无失败或错误。
$PY tools/run_main_engine_checks.py
# exit 0 / 24.402s；211 passed。
gitleaks dir . --config .gitleaks.toml --redact
# exit 0 / 2.514s；未发现凭据。
git diff --check
# exit 0。
```

公开副本中另有历史 checks-e1-followup.xml，本轮未运行该组，其96项结果不计入上表。
在已运行测试的副本复验清单时 exit 1，首个差异为 checks-affordability.xml：
测试命令生成了本次 XML，而清单仍绑定历史 XML。该失败及本次全部测试输出保留。
另建未运行测试的独立发布副本后，清单校验 exit 0 / 0.226s，2581 文件通过；
gitleaks exit 0 / 1.855s。未将新 XML 覆盖回仓库，也未改写历史清单来接受测试输出。
Git HTTPS fetch 两次与 HTTP/1.1 ls-remote 分别超时；GitHub API 可读取远端，
传输结果与远端提交身份另行核验，网络超时不计为工程或原生测试失败。

## 未解决依赖

真实commit(false)及持久化副作用、原版内部锁/自然并发、非Java身份和store/XML/backup
仍未闭合。下一项从真实失败提交的最小对照入手；no-VM、Android-free与训练门独立保留。
