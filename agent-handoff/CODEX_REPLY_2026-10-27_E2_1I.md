# Codex 回执：E2.1-I 自然冷启动 PN 原生控制桥

基线 `e61fe488cccc9f5197acd0aecbd2b0321dc486a6`，阶段 SHA 以本文件对应提交和最终消息为准。
仅发布任务分支；main/origin/main 保持 `0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与结果

`$PY` 为已有项目 Python，`$RUN` 为本轮独占私有目录，`$OLD_RUN` 为历史严格门目录。
路径占位仅用于脱敏，实际 argv、退出码、失败和输出均在本机保留。

```sh
$PY tools/run_e2_observed.py --config "$RUN/baseline-final.config.json" --output "$RUN/baseline-final"
$PY tools/run_e2_observed.py --config "$RUN/original-final.config.json" --output "$RUN/original-final"
$PY tools/run_e2_observed.py --config "$RUN/replacement-final-3.config.json" --output "$RUN/replacement-final-3"
$PY tools/run_e2_observed.py --config "$RUN/replacement-final-4.config.json" --output "$RUN/replacement-final-4"
$PY tools/verify_e2_1i_cold_pn_bridge.py --manifest "$RUN/private-manifest-i-2.json" --output "$RUN/verification-2.json"
E2_1I_PRIVATE_MANIFEST="$RUN/private-manifest-i-2.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_pn_bridge.js
node --check tools/e2_1_return_observer.js
node --check tools/tests/e2_1i_cold_bridge_harness.js
$PY tools/run_public_checks.py
$PY tools/run_main_engine_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

四个最终 run 均 exit 0；每进程 3,602 快照、28 动作、tick 3,601 终局、9,120,440 bytes
全字节一致，snapshot/restore/reset 回执一致。资源实际平台事件、Provider failures 和
live assets 均为 0。原九项输入/990 update 身份保持，桥库/dex 复编和 guest 身份一致；
现有 NDK/JDK/d8 构建 11 条命令 exit 0。

14,651 附件限定验证 exit 0；E2 全组 280 passed / 218 skipped / 29.21s / exit 0。
JS 语法检查 exit 0。历史 strict gate 重验 exit 2/BLOCKED；旧 manifest 和旧输出保留。
公开检查 exit 0：主组 794 passed / 27 skipped / 49.89s；其余八组通过数依次
23、82、15、25、31、31、38、65，跳过数依次 0、0、3、4、3、2、3、0。
主工程 211 passed / 20.54s / exit 0；gitleaks exit 0。
缺私有附件的跳过不作为真实实验通过，集合重叠不累加。生成检查 XML 私有归档，历史公开 XML 恢复原字节。

每次替换 138 次冷桥入口，133 次非 PN 原桥 fallback，5 条 PN 事件；其中自然 read/write
2 条、显式 readback 3 条。自然 read 无 commit，write 的 o/p 各 commit(true) 1 次，
真实转发顺序与原桥一致。独立 caller/线程/内部阶段关联，跨进程读回一致。
onLeave=0 保留，以原调用点返回指令完成窗口，未伪造零返回或零 fallback。

释放自有 Java 引用 6 个、JNI global 分配/删除各 1；本轮实例已回收，专用 listener=0，
历史 adb 身份保持。前两次 cold-ready 停滞及 detach、验证器 source 字段遗漏、
初次专项失败、辅助命令失败全部保留，见 failure index。

## 未解决依赖与下一步

自然 PN 控制逻辑已接管；Java 密钥派生、Cipher/XML writer 和 VM 保留。
首个未解决依赖为原密钥来源及原生持久化交接，下一步仍在 E2.1 内进行差分实验。
backup 交付、并发/崩溃持久化、46 键可移除性和初始化 ABI 保持 unknown。
移除键 0，未同时启用 32 键替换；没有 no-VM、Android-free、训练或 10× 通过声明。

完整报告：`E2_1I_COLD_PN_NATIVE_BRIDGE_2026-10-27.md`。
