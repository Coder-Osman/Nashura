# Codex回执：历史padding恢复序列与store0参考（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。
基线：`479280d9a17db0a059b9e424b57aab52fb16d0ba`。
本阶段commit SHA由本文件Git历史和最终交付消息定位，避免文件自引用。

| 项目 | 状态及实际结果 |
|---|---|
| 原版result专用胶囊、序列probe、身份来源核验 | IMPLEMENTED，8份新增源码/测试、2份修改工具 |
| 历史store1 padding异常/恢复 | RUNTIME_OBSERVED / 限定PASS，两份归档各两个新Bionic子进程，四条字节及canary全匹配 |
| 历史store0正常KEY/ENC/DEC | RUNTIME_OBSERVED / 限定PASS，两份归档各两个新Bionic子进程，每child30条匹配 |
| 实际输入和映射 | 各187项输入前后SHA一致，9项真实映射核验，没有libg映射 |
| XML还原 | 条目恢复；字节为[true,false]，保留store1序列化顺序差异 |
| 工程专项 | UNIT_PASS，378 passed、1 skipped |
| 当前平台契约 | UNRESOLVED：身份、完整异常、Java字符串/Base64、storage/backup |
| 严格catalog / ResourceManager | BLOCKED，CATALOG_READY_PASS=false |
| GameState、空场/reset、真实Step、动作、snapshot、完整局、Android逐tick | NOT_RUN |
| 无JavaVM生命周期、Android-free、10×、正式训练 | NOT_RUN |

本阶段未重新加载libg，未启动Android/ART/UI，未更改VM配置、冻结任务或main。
没有公开身份原文、key/IV、XML值、DEX、二进制、资源、私有内存或原始日志。
历史自然ready及旧失败记录保留，不改变其证据边界。

## 实际命令、退出码与结果

下面的私有路径占位对应真实运行argv，不是新增合成原生证据。完整参数模板见
[实验报告](PURE_LINUX_PREFERENCE_SEQUENCE_2026-10-28.md)。
逐child日志、构建、参考输入、胶囊、实际映射及回归输出SHA见
[机器证据](pure-linux-preference-sequence-evidence-20261028.json)。

| 实际入口或命令 | exit | 结果 |
|---|---:|---|
| NDK `aarch64-linux-android30-clang -Wall -Wextra -Werror -fPIE -pie ...` | 0 | v106/v107构建回执均成功，输出独占 |
| `preference_sequence_capsule.py --reference ...`，两份stream-diff归档 | 0 | 仅取original result，不读取native/stream输出作为expected |
| 同入口，缺原始ciphertext的direct-cipher归档 | 1 | 显式MISSING_INPUT；首次KeyError及后续ValueError失败保留 |
| `preference_sequence_job.py --expected-records 4 ...` | 0 | 每份wrapper0、child0/0；16→异常输出0→32→16 bytes全匹配 |
| `preference_historical_identity.py --dex ... --reference ...` | 0 | 两份历史原版身份记录与当前DEX有界图绑定；未获取当前运行身份 |
| `preference_reference_capsule.py --store 0 ...` | 0 | 两份参考各30条正常路径记录 |
| `preference_reference_job.py --expected-records 30 ...` | 0 | 两份wrapper0、child0/0，每child30条匹配 |
| `PYTHONDONTWRITEBYTECODE=1 python3.13 -m pytest -q -p no:cacheprovider tools/pure_linux --junitxml=...` | 0 | 378 passed、1 skipped |
| `python3.13 tools/run_main_engine_checks.py` | 0 | 211 passed |
| `python3.13 tools/run_public_checks.py` | 0 | 九组1,104 passed、42 skipped、零failure/error |

三组回归在v108独立副本运行，彼此有重叠，不累加。
248份工作树已跟踪XML与HEAD一致，未覆盖历史测试输出。
v106的32项及v107的30项VM操作回执均exit0；传输成功不代表语义等价。
guest为既有Ubuntu ARM64，aarch64、Linux6.8.0-138-generic；运行库及probe SHA见机器证据。

保留首次store选择测试1 failed/95 passed，修复循环局部变量遮蔽后通过。
宿主OpenSSL3失败后字节与Bionic参考存在差异，测试保留整体BLOCKED及后续恢复，
未修改核心实现来抹去不匹配。候选-3与Java异常类型的全域等价仍UNRESOLVED。

## 首个未解决依赖与下一步

首个前置依赖仍是**当前配置平台契约**，不是guest、APK或dlopen。
已闭合的是历史身份的正常字节及一条有界异常恢复序列；当前身份、空DEC、
Java UTF8/Base64、SharedPreferences分步commit/.bak/fsync/listener和BackupManager
外部副作用仍需真实证据。先绑定当前loading键的来源及存储调用链，再用两个
新libg子进程重验严格catalog。前置门通过后才进入ResourceManager和GameState。

发布清单检查`python3.13 tools/check_public_snapshot.py`退出0，核验2,439份文件；
`gitleaks dir . --config .gitleaks.toml --redact`退出0，扫描40.71 MB、未发现凭据；
`git diff --check`退出0，无删除项。10份本阶段源码与v108测试副本逐字节相同。
暂存检查及远端SHA核验结果见最终交付消息。
