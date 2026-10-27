# Codex 回执：E2.1-K 自然 PN 原生派生与 AES

基线 `dba6d81bf8ea58413f83123d0a108955891e0f6b`；阶段 SHA 以本文件所在提交及
最终答复为准。仅更新任务分支，main/origin/main 保持原值。

## 实际命令与结果

`$PY` 为既有项目 Python，`$RUN` 为本轮独占私有目录，`$OLD_RUN` 为历史门目录。
占位路径用于脱敏，实际 argv、输出和退出码留本地；完成标记使用独占新文件。

```sh
$PY tools/run_e2_observed.py --config "$RUN/baseline-final.config.json" --output "$RUN/baseline-final"
$PY tools/run_e2_observed.py --config "$RUN/original-final.config.json" --output "$RUN/original-final"
$PY tools/run_e2_observed.py --config "$RUN/replacement-final-3.config.json" --output "$RUN/replacement-final-3"
$PY tools/run_e2_observed.py --config "$RUN/replacement-final-4.config.json" --output "$RUN/replacement-final-4"
$PY "$RUN/complete_evidence.py"
$PY "$RUN/freeze_closed_evidence.py"
$PY tools/verify_e2_1k_natural_codec.py --manifest "$RUN/private-manifest-closed.json" --output "$RUN/verification-closed.json"
E2_1K_PRIVATE_MANIFEST="$RUN/private-manifest-closed.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_pn_native_backend.js
node --check tools/e2_1_pn_bridge.js
node --check tools/e2_1_return_observer.js
```

四 run 各 exit 0、3,602 快照、28 动作、9,120,440 bytes、tick 3,601 终局；直接字节
与 17 项非 status 回执一致。两替换各自然读/写一次、三个显式读回，双 store 各一次
真实 commit(true)，跨进程读回一致。133 次非 PN fallback 各自保留。
原 Java 读回前后 XML 字节不变；Java 身份、store 构造、XML、backup、VM 保留。

ART 六个 raw 根内具名 Cipher/旧 store 调用零，但全局 Cipher=262，另 8 个 opaque
method IDs 语义 unknown，非穷尽覆盖。不能据此宣称 JNI 或 Cipher 全局消失。
原输入、990 update、自有复编与 guest 产物身份通过；三复编命令 exit 0。

14,707 附件限定验证 exit 0；manifest SHA256：
`ae0f4b02085ec05db0785ac1559f3b533c33caced241594a8d33bf2f997c4dff`。
E2 全组 338 passed / 278 skipped / 16.06s / exit 0；三个 JS 语法检查 exit 0。
历史 strict gate 原清单重验 exit 2；K 自身 strict_gate 仍 BLOCKED。
独占实例/adb 已释放、专用 listener=0、历史 adb 身份保持。
初次 cleanup 证据缺失、trace 未结束、重复 metadata 拒收与测试导入失败均保留。

## 发布验证

完整发布集合在隔离副本中验证，避免测试输出覆盖工作树的历史证据：

```sh
$PY tools/run_public_checks.py
# exit 0，175.248s；主组 794 passed / 27 skipped。
# 其余八组依次为 23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、65。
$PY tools/run_main_engine_checks.py
# exit 0，211 passed，命令总耗时 24.494s。
$PY tools/check_public_snapshot.py
# exit 0，2,551 个文件通过。
gitleaks dir . --config .gitleaks.toml --redact
# exit 0，no leaks found。
git diff --check
# exit 0。
```

上述测试集合有重叠，不累加；仅后补发布结果文字并刷新源码清单。
daybreak/clapha 只读核验一并保留，外部引擎仍未运行，不与本轮原生证据混淆。

## 首个依赖与下一步

非 Java 身份来源、原 store 构造/XML/backup、失败及并发语义仍未闭合。
46 键移除 0，可移除性 unknown；整体 E2.1、no-VM、Android-free 分别保持未通过。
下一步补失败/回退/并发差分后再推进身份/存储交接；未启动正式训练或 10×。

完整报告：`E2_1K_NATURAL_PN_NATIVE_CODEC_2026-10-27.md`。
