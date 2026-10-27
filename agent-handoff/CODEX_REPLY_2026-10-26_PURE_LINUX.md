# Codex 回执：E2.1-N 实际续验（2026-10-27）

分支 `codex/pure-linux-libg-headless-20261026`；源基线
`82b94be5ae5ceb52f8cee0f5873ec3b8845bb573`。本回执与实现一同提交，发布 commit SHA
以包含本回执的 Git 提交记录和最终回复为准，避免文档自引用 SHA。
main/origin main 基线为 `0012201830cfbfcbfeb3d9dccd91a855028da869`，没有修改或合并。

## 逐项状态

| 项目 | 状态 | 实测/首个未解决点 |
|---|---|---|
| 环境与 Git 基线 | IMPLEMENTED | Darwin arm64/macOS15.6；任务分支与远端起点一致 |
| 可用 guest | BLOCKED | Fusion list=0；现有 VM 有挂起状态，未恢复/启动/改动；guest uname/网络未知 |
| 输入与 ELF 审计 | IMPLEMENTED | 匹配 libg；214/214 sysroot、990/990 资源；185 个 ARM64 静态候选库 |
| 原版 APK | MISSING_INPUT | 本地候选版本及嵌入 libg 相同，但整包 SHA 不等于锁定原包 |
| 专项单元测试 | UNIT_PASS | 15 项、exit0；全部为工程或合成测试 |
| 两种探针交叉构建 | IMPLEMENTED | direct Linux/Bionic ELF，exit0；没有执行 Linux ELF |
| 实际 Darwin 守门 | UNIT_PASS | runner exit2/BLOCKED_ENVIRONMENT；未执行探针 |
| guest Linux/Bionic smoke | BLOCKED / NOT_RUN | 缺少已验证 live guest 会话 |
| 原版 libg load | NOT_RUN | 尚无 LOAD_ONLY 证据 |
| 真实生命周期/状态对照 | NOT_RUN | 非 NATIVE_VERIFIED；独立内容/manager 初始化仍待验证 |
| 无 VM / Android-free / 10× | NOT_RUN | 未启动，未授予 PASS |
| 正式训练/放量 | NOT_RUN | 冻结部署和已有任务保持原状 |

本轮动态阶段的直接阻塞为 guest 会话缺失；后续真实原版阶段另受原包身份缺口约束。
两个问题分别登记，不把环境限制解释为原版战斗逻辑不可剥离。

## 实际命令与退出码

以下变量为公开脱敏路径；完整 argv、stdout/stderr、私有输出 SHA 均留在本轮私有目录
`pure-linux-20261027-flyxngxb`。机器可读命令索引见 `dependency-report.json`。

| 实际命令 | exit | 结果 |
|---|---:|---|
| `uname -s` / `uname -m` / `sw_vers` / `sysctl -n hw.memsize` | 各0 | Darwin / arm64 / 15.6 / 38,654,705,664 bytes |
| `git status --short --branch` / `git rev-parse HEAD` / `git branch --show-current` | 各0 | 保留原未提交草稿及工具，不覆盖其他树 |
| `vmrun list` | 0 | 0 台运行 VM |
| `aapt dump badging <local-candidate.apk>` | 0 | 包与版本匹配，整包 SHA 单独核验后不匹配原包 |
| `llvm-readelf -h -l -d -Ws "$BASELINE/app-libs/libg.so"` | 0 | ELF64/AArch64/ET_DYN，12 PH/5 SH，无 PT_INTERP |
| `llvm-nm -D ...` / `llvm-objdump -T ...` | 各0 | 空或有限符号输出；另用 PT_DYNAMIC 解析 |
| `python3 tools/pure_linux/audit_inputs.py --baseline "$BASELINE" --supported-engine firstlight_CR/native_runner/supported_engine.json --output "$EVIDENCE/input-audit-v3.json"` | 0 | 185 ARM64 候选，无静态缺库或同 ABI 歧义；runtime NOT_RUN |
| `python3 -m unittest discover -s tools/pure_linux -p test_audit.py -v` | 0 | 15 tests OK |
| direct Linux / Bionic 交叉构建 | 各0 | 完整命令见 ABI 报告 |
| `python3 tools/pure_linux/run_arm64_smoke.py --elf "$EVIDENCE/arm64-linux-smoke" --sha256 bbdaddebf64c3b662584b6d0a537656165d83570afc91cc9d7c05b7b44fc603b --output "$EVIDENCE/mac-host-guard"` | 2 | 实际 Darwin 守门，未运行 ELF；此条使用保留的第一版构建 |
| `"$CHECK_PYTHON" tools/run_main_engine_checks.py` | 0 | 隔离起始 tracked snapshot：211 passed，21.64s |
| `"$CHECK_PYTHON" tools/run_public_checks.py` | 0 | 同一隔离 snapshot：主组794 passed/27 skipped，49.09s |

公开入口其他组：23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、65；
这些组与其他工程检查有交叠，不求和。缺私有原生附件的 skip 不替代原生验收。
该 snapshot 来自起始 tracked HEAD，不含新增 untracked 工具；新增工具由15项专项覆盖。
发布检查实测：`python3 tools/check_public_snapshot.py` exit0，2,345 个源文件 SHA/大小一致；
`gitleaks dir . --config .gitleaks.toml --redact` exit0，扫描约39.21 MB、未发现泄漏；
`git diff --check` exit0。上述为发布检查，不提升原生验收门。

## 输入、输出与日志身份

- 输入 libg SHA：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- runtime fingerprint SHA：`e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339`。
- 原包锁定 SHA：`685c115ee432d5fd2d7be856a58ea0cb029c9c187aa9061f3cd250994dfbcf2a`，MISSING_INPUT。
- direct smoke-v3 SHA：`2249f3439c240dd6bde1aeb8652a6c1de98a04bb1fdf30f3a9f7054af9b6e8b1`。
- Bionic probe-v2 SHA：`02dbc40e7ff04eafd633d24b1c8bd69fe88959d9105c5cb8cfe4e78c7a8db9cc`。
- 私有 input-audit-v3 SHA：`8cbeace2f7af6051fe2c2bc525958e39f1dd19edceffff85e6fc68281ab9f136`。
- Darwin guard report SHA：`b355b06849e348795722beb4b7a69facd80cae3e2f43e63328c8b497274a9fd6`。
- 主工程 stdout：`e3d7bef6b4453bbcb28018d8594537e31a4e8079c302620050b50cab3026a7c6`。
- 公开入口 stdout：`bc6e8b1ff5723e00fa4c5e230bb97deefed1e8d44e41a8f23f5d6bb1f3af7029`。
- 15项专项 stderr：`0fdf719d58f227c9e790071dd64abaceb38e6b4fc3979d2515b1b4fc28aeeb2d`。

完整日志相对名、各命令 exit 和 stdout/stderr SHA 在 JSON 中；原始 ELF 审计、汇编、资源、
机器配置及二进制不公开。初次 tar 解压失败、旧 ABI 误分类报告与旧构建保留，不重写历史。

## 首个依赖与下一步

先取得一个独立可用 ARM64 Linux guest 会话，记录 live uname/系统/网络与隔离根身份，
执行 direct 合成 smoke；再不带 libg 参数运行 Bionic 探针。原包/资源身份完全核验后才做
真实 dlopen，并区分 interpreter、Bionic、namespace、缺库/符号、JNI/资源/图形和内容初始化。
内容上下文创建与 manager/state 初始化是随后首项逻辑闭包实验。

最终判定：**BLOCKED_ENVIRONMENT；工程实现与静态审计完成，原版无 VM 生命周期尚未验证。**
