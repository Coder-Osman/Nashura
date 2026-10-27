# Codex 回执：E2.1-J PN 原生派生与 Cipher 差分

基线 `203357dc6eaf6416acb48ac14cefe3dbeb0e8dd4`；阶段 SHA 以本文件对应提交及最终消息为准。
仅发布任务分支 `codex/headless-engine-plan-20261025`，main/origin/main 保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与结果

`$PY` 为已有项目 Python，`$RUN` 为本轮独占私有目录，`$OLD_RUN` 为历史严格门目录。
占位路径用于脱敏；实际命令、argv、退出码、日志及附件在本地保留。

```sh
$PY tools/run_e2_observed.py --config "$RUN/baseline-final.config.json" --output "$RUN/baseline-final"
$PY tools/run_e2_observed.py --config "$RUN/derivation-final-1.config.json" --output "$RUN/derivation-final-1"
$PY tools/run_e2_observed.py --config "$RUN/derivation-final-2.config.json" --output "$RUN/derivation-final-2"
$PY "$RUN/cleanup_instance.py"
$PY "$RUN/freeze_closed_evidence.py"
$PY tools/verify_e2_1j_pn_derive.py --manifest "$RUN/private-manifest-closed.json" --output "$RUN/verification-closed.json"
E2_1J_PRIVATE_MANIFEST="$RUN/private-manifest-closed.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_pn_derive_probe.js
node --check tools/e2_1_return_observer.js
$PY tools/run_public_checks.py
$PY tools/run_main_engine_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
git diff --cached --check
```

三最终 run exit 0，各 3,602 快照、28 动作、tick 3,601 终局、9,120,440 bytes 全字节一致，
snapshot/restore/reset 与 17 项非 status 控制回执一致。原输入及 990 个 update 身份保持；
libg 九槽、FMOD 六绑定通过；实际平台资源事件/Provider failures/live assets 均为 0。

两次派生各 40 项原 Cipher 对照和 5 项 guest 无效输入通过，host C/guest C/hashlib 一致，
XML 原字节不变。矩阵只在完整轨迹后执行，原初始化与原 Java Cipher/XML writer 均未替换。
5 条复编命令 exit 0，4 项产物 SHA 一致，实验结束后的 guest 产物身份复核通过。

回收后封存 22,058 附件，限定验证 exit 0，manifest SHA256 为
`9eab1cd6f1321381d7100701379e04da48e57f226a53bd28a2976b8ea079266e`。
E2 最终全组 299 passed / 272 skipped / 10.90s / exit 0；主工程 211 passed / 24.47s / exit 0。
公开检查 exit 0：主组 794 passed / 27 skipped / 58.14s；其余八组通过数依次
23、82、15、25、31、31、38、65，跳过数依次 0、0、3、4、3、2、3、0。
两 JS 语法检查 exit 0。跳过不计原生通过，集合重叠不累加；公开 XML 私有归档后恢复原字节。
历史 strict gate exit 2/BLOCKED，不变更旧门或旧失败报告。
源码清单核验 2,534 文件通过；gitleaks exit 0，未发现凭据；工作区 diff 检查 exit 0。

本轮实例释放 exit 0，专用 listener=0，历史 adb 身份保持。
两次白名单拒绝、工程 fixture 编译失败、过早封存和辅助读取路径错误均保留，见 failure index。

## 首个未解决依赖与下一步

已闭合的是显式输入的原生派生与原 Cipher 差分。自然初始化尚未接管，身份仍由 Java 获取；
原生 XML writer、backup 交付、并发/崩溃及 46 键可移除性仍 unknown，移除数 0。
下一步限定 E2.1 的初始化集成和身份来源契约；不启动 no-VM、Android-free、训练或 10×。

完整报告：`E2_1J_PN_NATIVE_DERIVATION_2026-10-27.md`。
