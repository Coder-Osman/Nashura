# Codex回执：配置控制流与平台契约（2026-10-28）

分支：`codex/pure-linux-libg-headless-20261026`。
基线提交：`0a5f8fac037691c1587554a5c002275cd243e15a`。
本阶段提交由本文件的Git历史和交付消息定位，避免文件自引用提交SHA。

## 本阶段状态

| 项目 | 状态及实际结果 |
|---|---|
| 初始化有界图 | STATIC_FOUND；142节点、15分支及数组相同；完整onCreate仍不同 |
| 单键分步配置候选 | IMPLEMENTED / UNIT_PASS；显式codec、双store、commit及backup边界 |
| 真实DEX受限解释差分 | 当前、历史各4,522例零差异；外部平台边界仍为共享合成模型 |
| 首次差分失败 | v89 exit2、252差异保留；修正adapter解密异常映射后独立重验 |
| ARM64/Bionic候选 | RUNTIME_OBSERVED；两个child exit0、15故障边界/8场景组 |
| 真实libg及严格catalog重验 | NOT_RUN；候选未接入libg，CATALOG_READY_PASS=false |
| ResourceManager | BLOCKED |
| GameState、空场/reset、真实Step、动作、终局及Android对照 | NOT_RUN |
| 无JavaVM生命周期、Android-free、训练、10× | NOT_RUN |

详细命令、输入及输出SHA、日志摘要见
[报告](PURE_LINUX_PREFERENCE_SEMANTICS_2026-10-28.md)和
[机器证据](pure-linux-preference-semantics-evidence-20261028.json)。
公开内容仅含实现、测试和脱敏摘要，不含DEX、密码材料、资源或原始日志。

## 实际执行与退出码

环境：已有Darwin ARM64、Python 3.13.5；工程回归在独立仓库副本执行，
不覆盖原工作树的历史测试输出。未安装依赖或启动Android。

| 实际命令 | exit | 结果 |
|---|---:|---|
| `python3.13 tools/run_main_engine_checks.py` | 0 | 211 passed |
| `python3.13 tools/run_public_checks.py` | 0 | 九组1,104 passed、42 skipped、0 failure/error |
| `python3.13 -m pytest -q -p no:cacheprovider tools/pure_linux` | 0 | 216 passed、1 skipped |
| `clang -shared -fPIC -Wall -Wextra -Werror tools/pure_linux/preference_contract.c -o "$C_LIBRARY"` | 0 | 新候选构建 |
| `preference_initializer_audit.py`（完整参数见报告） | 0 | 新目录v93两输入有界图一致 |
| `preference_differential.py`（完整参数见报告） | 0/0 | v93当前及历史各4,522例零差异 |
| `preference_contract_job.py`（完整参数见报告） | 0 | v91两个ARM64/Bionic子进程通过，未加载libg |

ARM64环境为既有Ubuntu22.04.5/aarch64/Linux6.8.0-138-generic。
probe SHA：`100be4061e854c6d8457c3dec650810c9c403bd0a61382a25b67aef16280f0e1`。
187项payload/probe身份前后保持一致；实际映射库完整清单未观测。
本轮250份历史XML逐字节未变，保护清单SHA：
`7461717e7805ff41f84fb216925faefff6fa04f188b5cff78f6983b0b6f116bf`。

发布检查：`python3.13 tools/check_public_snapshot.py` exit0，核验2,408个文件；
`gitleaks dir . --config .gitleaks.toml --redact` exit0，无泄露；`git diff --check` exit0。
首次`git diff --cached --check` exit2，发现新文件`preference_model.py`末尾多余空行；
随后只移除该空行并更新SHA，保留首次失败记录。功能回归对应空白清理前字节。
最终暂存检查和发布状态在交付消息及私有发布回执保留。
本轮只提交、推送当前任务分支，main及冻结部署保持原样。

## 首个未解决依赖与下一步

**唯一首个前置阻塞为真实配置平台契约，不是缺APK或ARM64 guest。**
需要闭合当前loading键的真实初始化身份、AES/Base64/UTF8与持久Cipher错误状态、
SharedPreferences内存/磁盘及分步commit失败、XML/crash和backup副作用。
历史PN证据只用于界定研究方向，不作为当前键及Linux平台的动态等价证据。

下一最小实验为独立绑定当前键真实codec及状态机，再验证存储和backup边界。
这些前置通过后才允许接入配置桥，在两个新libg子进程重验严格catalog门；
严格门通过前不调用ResourceManager、GameState或GameStateStep。
