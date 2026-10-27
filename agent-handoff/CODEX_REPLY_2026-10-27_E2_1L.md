# Codex 回执：E2.1-L PN 异常与持久 Cipher

基线 `a492c85990dee9e83463cbd61ad5698d48355b95`。阶段 SHA 以本文件所在提交和
最终回执为准；仅发布任务分支，main/origin/main 保持原值。

## 实际命令

`$PY` 为既有项目 Python，`$RUN` 为本轮独占私有目录。实际 argv、日志、私有配置
及原始证据留本地；以下路径变量用于脱敏，命令结果未以合成 fixture 代替。

```sh
$PY tools/run_e2_observed.py --config "$RUN/<run>.config.json" --output "$RUN/<run>"
# 九个成功 run；两次早期 stream 尝试 exit 1，均保留独立目录。
$PY "$RUN/prepare_verification.py"
# exit 0，clang / javac / d8 全部复编、SO / DEX 字节一致。
$PY tools/verify_e2_1l_edges.py --manifest "$RUN/private-manifest.json" --output "$RUN/verified-edges-1.json"
# exit 0，33058 附件，bounded_diagnostic_verified；strict BLOCKED。
E2_1L_PRIVATE_MANIFEST="$RUN/private-manifest.json" $PY -m pytest tools/tests/test_e2_1l_edges.py -q --junitxml="$RUN/tests-l-1.xml"
# exit 0，42 passed / 7.12s。
E2_1L_PRIVATE_MANIFEST="$RUN/private-manifest.json" $PY -m pytest tools/tests -q -k e2 --junitxml="$RUN/tests-e2-1.xml"
# exit 1，1 failed / 348 passed / 309 skipped；Java 测试桩编译缺 API。
E2_1L_PRIVATE_MANIFEST="$RUN/private-manifest.json" $PY -m pytest tools/tests -q -k e2 --junitxml="$RUN/tests-e2-2.xml"
# 修正桩声明后 exit 0，349 passed / 309 skipped / 149 deselected / 12.03s。
```

九 run 完整轨迹均 3602 快照、28 动作、9120440 bytes、终局 tick 3601，17 非 status
控制回执全等；原输入/990 update 身份保持，平台资源事件/Provider failures/live assets 零。
异常矩阵全部在完整轨迹之后，**异常后完整局尚未验证**。

两次独立 native stream 差分共 16 次：short-block 后失败保留，padding 后先输出
32 bytes 再恢复 16 bytes，所有状态和输出字节匹配原 Cipher。异常类别对应 unknown，
只观察到 JNI bridge 无 pending exception，不称为内部 Java 无异常。
原 XML 按语义恢复，非 PN 条目保持；本轮不是自然替换，Java/VM/XML 依赖保留。

游戏所属进程已核验并清理；独占 AVD/adb 留给后续实验，历史进程保持。
失败证据见 `e2_1l_failure_index.json`。公开附件摘要见 `e2_1l_evidence_summary.json`。

## 发布验证

在独立发布副本实际执行，未覆盖仓库内历史 XML：

```sh
$PY tools/run_public_checks.py
# exit 0，164.08 秒；完整分组日志保留私有实验目录。
$PY tools/run_main_engine_checks.py
# exit 0，21.14 秒。
gitleaks dir . --config .gitleaks.toml --redact
# exit 0，1.70 秒。
$PY tools/check_public_snapshot.py
git diff --check
# 均 exit 0；提交前再次核对源码清单与暂存 diff。
```

首个发布副本复制因 Git 中文文件名转义 exit 1，失败目录保留；改用 NUL 分隔的
`git ls-files -z`，在第二个独立目录完成上述检查。该辅助失败不是引擎失败。

## 未解决项

PN 持久状态尚未接真实替换；异常后完整局、Java 身份、store/XML/backup、
commit-false、并发及崩溃契约待验。46 键移除 0、可移除性 unknown。
strict、no-VM、Android-free、训练与 10× 均未通过或启动。
