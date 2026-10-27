# Codex回执：持久Cipher与原版正常路径字节参考（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。
基线：`db684e816e9cad1f2ebb03cae1b89c9938f00b53`。
本阶段commit SHA由本文件Git历史和交付消息给出，避免文件自引用。

| 项目 | 状态与实际结果 |
|---|---|
| 持久AES候选及身份/参考工具 | IMPLEMENTED，17份新源码和测试 |
| 工程专项 | UNIT_PASS，304 passed、1 skipped；含88项本阶段新增测试 |
| ARM64/Bionic primitives | RUNTIME_OBSERVED，两个child exit0，2首块/20边界组合及错误序列 |
| 两条历史XML加密键 | 限定PASS，两个child各2条；只作KEY字节参考 |
| 历史原版正常密码路径 | 限定PASS，两个新child各30条，187输入未变、9实际映射核验 |
| 首次采集器失败 | v97保留，child0/0、collector1；逻辑linker路径不是真实文件路径 |
| 当前配置完整语义 | UNRESOLVED，身份、异常、storage与backup仍是前置 |
| 严格catalog / manager | BLOCKED，CATALOG_READY_PASS=false |
| GameState / Step / 战斗 / Android逐tick对照 | NOT_RUN |
| 无VM生命周期 / Android-free / 10× / 训练 | NOT_RUN |

没有重新加载libg，没有启动Android、ART或UI，没有更改冻结任务、VM配置或main。
未公开DEX、key/IV原文、XML值、资源、私有内存及原始日志。
完整实验说明、命令与证据：
[报告](PURE_LINUX_PREFERENCE_CIPHER_2026-10-28.md)、
[机器证据](pure-linux-preference-cipher-evidence-20261028.json)。

## 实际命令及退出码

| 命令或入口 | exit | 实际结果 |
|---|---:|---|
| host及NDK编译密码候选/两probe | 0 | 新隔离输出；SHA见机器证据 |
| `preference_identity_material.py`，完整argv见报告 | 0 | 真实DEX的p身份静态提取；运行时获取未闭合 |
| `preference_reference_capsule.py`，完整argv见报告 | 0 | 从历史original字段构造30条expected，排除native字段 |
| `preference_cipher_job.py`，完整argv见报告 | 0 | 两个新child0/0；异常结果仅候选观察 |
| `preference_reference_job.py --expected-records 30 ...` | 0 | 两个新child0/0；逐字节匹配历史正常参考 |
| `python3.13 -m pytest -q -p no:cacheprovider tools/pure_linux` | 0 | 304 passed、1 skipped |
| `python3.13 tools/run_main_engine_checks.py` | 0 | 211 passed |
| `python3.13 tools/run_public_checks.py` | 0 | 九组1,104 passed、42 skipped、零失败错误 |

三组回归在v105独立副本执行，集合不累加。工作树248份已跟踪XML与HEAD逐字节相同。
guest：既有Ubuntu22.04.5、aarch64、Linux6.8.0-138-generic。
reference probe SHA：`ce5cd0e19c34ba23cefa68437f7af3775ec6262814ff50648d445cad125f73a3`。
v104结果SHA：`619e5fa51e2950d2a6999face5fb42f0931dbc1f752fdeed96b1b2f41843c7dd`。
发布检查：`python3.13 tools/check_public_snapshot.py`退出0，核验2,428份文件；
`gitleaks dir . --config .gitleaks.toml --redact`退出0，扫描40.63 MB、未发现凭据；
`git diff --check`退出0，无删除项。17份新增源码与v105测试副本逐字节相同。
暂存检查和远端SHA核验结果见最终交付消息；本文件Git历史给出实际提交SHA。

## 首个未解决依赖

仍是**真实配置平台契约**，不是APK、guest或dlopen。
本阶段只闭合历史正常密码字节，当前初始化身份、同输入异常后状态、空解密、
Java UTF8/Base64、SharedPreferences分步持久化与backup外部副作用仍需验证。
正常字节一致不升级为当前loading键整链语义一致，也不升级为无VM战斗。
这些前置完成后再重验严格catalog；通过之前下游GameState保持NOT_RUN。
