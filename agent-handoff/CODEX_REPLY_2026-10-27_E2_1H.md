# Codex回执：E2.1-H PN控制逻辑原生桥

基线`65930c7337901611c48ebe9403e6f76ef717f74c`，阶段SHA以本文件对应提交及最终
消息为准，仅推任务分支。main/origin/main仍为`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与结果

`$PY`为已有项目Python，`$RUN`为本轮独占私有目录，`$OLD_RUN`为历史严格门目录，
`$CC`为现有NDK ARM64编译器。以下路径占位仅脱敏；实际argv/退出码/日志本地保留。

```sh
$CC -shared -fPIC -Wall -Wextra -Werror "$RUN/verification_sources/e2_1_pn_bridge.c" -o "$RUN/bridge-reproduction/libe21h_bridge.so"
$PY tools/run_e2_observed.py --config "$RUN/baseline-final.config.json" --output "$RUN/baseline-final"
$PY tools/run_e2_observed.py --config "$RUN/original-final.config.json" --output "$RUN/original-final"
$PY tools/run_e2_observed.py --config "$RUN/replacement-retained-1.config.json" --output "$RUN/replacement-retained-1"
$PY tools/run_e2_observed.py --config "$RUN/replacement-final-2.config.json" --output "$RUN/replacement-final-2"
$PY tools/verify_e2_1h_pn_bridge.py --manifest "$RUN/private-manifest-v2.json" --output "$RUN/verification-2.json"
E2_1H_PRIVATE_MANIFEST="$RUN/private-manifest-v2.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2_1_pn_bridge.py tools/tests/test_e2_1h_pn_bridge_evidence.py
E2_1H_PRIVATE_MANIFEST="$RUN/private-manifest-v2.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_pn_bridge.js
node --check tools/e2_1_return_observer.js
$PY tools/run_public_checks.py
$PY tools/run_main_engine_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

构建11条命令全部exit 0，桥库/dex复编及guest初末身份核对exit 0。
四个最终run均exit 0；14744附件限定验证exit 0；专项85 passed / 25.36s，
E2全组271 passed / 171 skipped / 28.26s；两份JS语法检查exit 0。
严格门exit 2。公开检查exit 0：主组794 passed / 27 skipped / 89.19s；
其余8组分别23、82、15、25、31、31、38、65 passed，显式skip分别0、0、3、4、3、2、3、0。
主工程211 passed / 21.23s / exit 0；gitleaks exit 0、no leaks found。
各测试集合有重叠，不累加为独立用例数；生成的检查XML私有归档，历史公开XML原字节恢复。

每进程3602快照/28动作/tick3601终局/9120440bytes全字节一致；
snapshot/restore/reset回执一致。实际平台资源事件0、Provider failures/live_assets均0；
原始9输入及990update身份不变。新增桥库/dex身份为整体窗口初末核验。

各矩阵11项，真实commit(true)7次，原XML逻辑值恢复且非PN保持；
两具名backup方法各5次正常返回，7种未命名方法/480records保持unknown。
两次替换有13条显式PN事件，自然PN未覆盖；非PN回退分别0和31，键名unknown。
限定验证通过不消除46键、原冷启动/key来源、Java存储、并发和崩溃持久化缺口。

保留RPC错误两次、GC访问异常一次、验证器错误要求非PN回退0和负测fixture失败。
最终独占实例回收exit 0，专用端口listener=0，历史adb身份保持；AVD和证据保留。
无E2.2/no-VM、Android-free、训练、放量或10×通过声明。

下一步仍为E2.1原自然冷启动接入和原后端契约，保留VM，逐项验证而非统一默认值。
