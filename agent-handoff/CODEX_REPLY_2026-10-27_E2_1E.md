# Codex 回执：E2.1-E

完成 PN_POPUP 独立原生存储候选和 post-trajectory 差分；严格 G-E2.1 保持 BLOCKED。
起点 `8c000dd73b3d342199d765db33534dbca2766f37`；阶段 SHA 以本文件对应提交及最终消息为准。
仅发布任务分支，main/origin/main 保持 `0012201830cfbfcbfeb3d9dccd91a855028da869`。

## 实际命令与退出码

`$PY` 为已有项目 Python，`$RUN` 为本轮独占私有目录，`$OLD_RUN` 为原严格门附件目录，
`$CC` 为已有 NDK 的 aarch64-linux-android30-clang。以下占位符仅对公开路径脱敏；
本机已保存实际 argv、退出码、stdout/stderr，输出均独占创建。

```sh
$CC -std=c11 -shared -fPIC -O2 -Wall -Wextra -Werror tools/e2_1_pn_store.c -o "$RUN/libe21pn.so"
$CC -std=c11 -shared -fPIC -O2 -Wall -Wextra -Werror tools/e2_1_pn_store.c -o "$RUN/libe21pn-rebuilt.so"
$PY tools/run_e2_observed.py --config "$RUN/baseline.config.json" --output "$RUN/baseline"
$PY tools/run_e2_observed.py --config "$RUN/candidate-1.config.json" --output "$RUN/candidate-1"
$PY tools/run_e2_observed.py --config "$RUN/candidate-2.config.json" --output "$RUN/candidate-2"
$PY tools/run_e2_observed.py --config "$RUN/candidate-3.config.json" --output "$RUN/candidate-3"
$PY tools/verify_e2_1e_candidate.py --manifest "$RUN/e2-1e-private-manifest-v1.json" --output "$RUN/bounded-verification-1.json"
E2_1E_PRIVATE_MANIFEST="$RUN/e2-1e-private-manifest-v1.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2_1_pn_store.py tools/tests/test_e2_1e_candidate.py
E2_1E_PRIVATE_MANIFEST="$RUN/e2-1e-private-manifest-v1.json" $PY -m pytest -q -p no:cacheprovider tools/tests/test_e2*.py
$PY tools/verify_e2_1_gate.py --manifest "$OLD_RUN/e2-1-private-manifest-v3.json" --output "$RUN/strict-gate-recheck-1.json"
node --check tools/e2_1_pn_candidate.js
node --check tools/e2_jni_config_observer.js
node --check tools/e2_config_replace.js
$PY tools/run_main_engine_checks.py
$PY tools/run_public_checks.py
$PY tools/check_public_snapshot.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
```

构建与复编 exit 0、字节一致；baseline exit 0；candidate-1 exit 1（SELinux 加载拒绝）；
candidate-2、candidate-3 exit 0。最终附件限定验证 exit 0；旧严格门 exit 2，条件未放宽。
首次宿主构建的宏错误与所有早期运行保留，不覆盖失败记录。

## 实际测试结果

- 专项：44 passed / 2.11s，包含真实私有附件正向和负向突变。
- E2 全组：175 passed / 74 skipped / 3.31s，skip 不计作原生通过。
- 主工程：211 passed / 23.09s。
- 公开主组：794 passed / 27 skipped / 49.55s。
- 公开八组：affordability 23；selection-timeout 82；position-guards 15 / 3 skipped；
  live-command 25 / 4 skipped；dispatch-command 31 / 3 skipped；reservation-mapping 31 / 2 skipped；
  command-coordinates 38 / 3 skipped；collection-coordinator 65，全部 exit 0。

测试集合有重叠，不相加。公开回归生成的九份 XML 私有归档后恢复历史字节。
JS三文件语法检查exit0；凭据扫描exit0、no leaks found，Git whitespace检查exit0。
源码清单与提交前检查结果以本阶段最终消息及 Git 提交为准。

## 原生证据与未解决依赖

baseline 与 candidate-3 各3602份原生快照、28动作和完整生命周期回执一致；
候选只在这些 tick 完成之后加载，未作为战斗配置桥使用。
原桥17行含保存/恢复，候选14行、7次read字节对应；真实EACCES验证内存更新、
磁盘不变、同值不重试，close/open及close后拒收通过。资源平台事件0/失败0/live0。
14,604附件与最终源码绑定，摘要仅白名单字段；无私有二进制、配置值或原始审计发布。

首个缺口：原桥集成与Cipher/XML存储交接。真实p/o冲突和回填差分、backup交付、
并发等价、崩溃恢复、46键可移除性仍unknown。独占实例已回收、历史adb保持。
下一步仍为保留VM的PN语义补验；未进入E2.2/no-VM、Android-free、训练或10×。
