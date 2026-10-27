# Codex回执：E2.1-G

完成原PN Cipher/XML限定格式兼容差分；G-E2.1严格门保持BLOCKED。
起点`c3e5b3cd3bdd29bfb31cc579a2174254c59b1eb3`；阶段SHA以本文件对应提交及最终消息为准。
仅发布`codex/headless-engine-plan-20261025`，main/origin/main保持
`0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与退出码

`$PY`为已有项目Python，`$RUN`为本轮独占私有目录，`$OLD_RUN`为历史严格门附件目录，
`$CC`为已有NDK的aarch64-linux-android30-clang。占位符仅脱敏路径；实际argv、
退出码、stdout/stderr与失败记录本地保留。输出使用独占目录/文件。

```sh
$CC -shared -fPIC -Wall -Wextra -Werror "$RUN/verification-sources-v1/e2_1_pn_codec.c" -o "$RUN/codec-rebuilt-v1.so" -ldl
$PY tools/run_e2_observed.py --config "$RUN/baseline-current.config.json" --output "$RUN/baseline-current"
$PY tools/run_e2_observed.py --config "$RUN/codec-1.config.json" --output "$RUN/codec-1"
$PY tools/run_e2_observed.py --config "$RUN/codec-2.config.json" --output "$RUN/codec-2"
$PY tools/run_e2_observed.py --config "$RUN/codec-system-1.config.json" --output "$RUN/codec-system-1"
$PY tools/run_e2_observed.py --config "$RUN/codec-system-2.config.json" --output "$RUN/codec-system-2"
$PY tools/run_e2_observed.py --config "$RUN/codec-scoped-1.config.json" --output "$RUN/codec-scoped-1"
$PY tools/run_e2_observed.py --config "$RUN/codec-scoped-2.config.json" --output "$RUN/codec-scoped-2"
$PY tools/verify_e2_1g_pn_codec.py --manifest "$RUN/e2-1g-private-manifest-v1.json" --output "$RUN/bounded-summary-v1.json"
$PY tools/verify_e2_1g_pn_codec.py --manifest "$RUN/e2-1g-private-manifest-v1.json" --output "$RUN/bounded-summary-recheck.json"
E2_1G_PRIVATE_MANIFEST="$RUN/e2-1g-private-manifest-v1.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2_1g_pn_codec.py tools/tests/test_e2_1_pn_codec.py tools/tests/test_e2_1_observer_release.py
E2_1G_PRIVATE_MANIFEST="$RUN/e2-1g-private-manifest-v1.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck.json"
node --check tools/e2_1_pn_codec_probe.js
node --check tools/e2_1_return_observer.js
$PY tools/run_main_engine_checks.py
$PY tools/run_public_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

构建/安装/guest身份核验12条命令exit0；NDK复编exit0，字节SHA与实际guest库一致。
baseline-current及两次codec-scoped运行exit0；四次较早运行exit1，分别为app linker
namespace拒绝和shell/app作用域身份差异，失败记录保留。两次限定验证均exit0。
严格门exit2；显式profile、原桥与writer保留，不外推native密钥派生或无VM。

回收首次身份字符串检查exit1，尚未执行回收；修正历史采集的strip格式差异后，
完整PID/启动时间/argv复核通过，仅回收本轮实例及其专用adb server。
独占端口listener为0，历史adb身份保持，失败证据和AVD目录保留。

## 实际测试结果

- 专项：72 passed / 2.17s，包含真实附件、31类证据突变、非PN XML变动拒收、路径/身份负向测试；
  C codec含NIST首block向量、边界与失败不输出部分明文的工程测试。
- E2全组：222 passed / 135 skipped / 3.54s；历史私有附件未提供的测试显式skip。
- 主工程：211 passed / 21.52s。
- 公开主组：794 passed / 27 skipped / 48.67s。
- 公开八组：affordability 23；selection-timeout 82；position-guards 15 / 3 skipped；
  live-command 25 / 4 skipped；dispatch-command 31 / 3 skipped；reservation-mapping 31 / 2 skipped；
  command-coordinates 38 / 3 skipped；collection-coordinator 65，全部exit0。

测试集合重叠，不累加；skip不计作原生通过。公开回归产生的XML私有归档后恢复
历史原字节。首次源码清单检查因本轮CHANGELOG等已更新、清单未刷新而exit1；
失败日志保留，只更新本轮公开源文件和新增文件身份，不运行全量导出覆盖其他源码。
最终源码清单检查2503文件exit0；暂存检查结果以阶段提交前记录及最终回执为准。
两个JS语法检查exit0，gitleaks扫描exit0（no leaks found）。

## 证据与首个缺口

三独立进程各3602快照、9120440bytes、28动作、tick3601终局及snapshot/restore/reset一致；
实际平台AssetManager事件0、Provider失败0、live assets0，原输入和990个update身份保持。
每次矩阵40组密文比较、80次native变换、20次原store读回、6项真实损坏清理通过。
真实XML逻辑条目恢复，非PN逐项保持；11129附件绑定最终执行源码与构建身份。

首个缺口为PN战斗桥集成、原XML writer交接和native密钥来源契约；backup交付、
并发等价、崩溃持久化、null/非法UTF-8及46键可移除性仍unknown。
原PN桥和writer未替换；未进入E2.2/no-VM、Android-free、正式训练或10×。
下一步仍保留VM，先明确持久化适配契约，再验证原自然调用及完整轨迹。
