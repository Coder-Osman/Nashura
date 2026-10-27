# Codex 回执：ARM64 Linux guest ABI 续验

分支 `codex/pure-linux-libg-headless-20261026`；父提交
`3326d9f9dc7838c37750dfc02fc7a16f38661e87`。包含本文件的提交为阶段发布身份。

## 已执行

| 项目 | 状态 | 命令及退出码 |
|---|---|---|
| 现有 Fusion VM 恢复及身份核验 | IMPLEMENTED | vmrun guest operations；Linux/aarch64/Ubuntu 22.04.5 已实测 |
| 首次 Linux smoke | BLOCKED，保留失败 | run_arm64_smoke.py v3：runner 1、程序 -11 |
| 修复 clone 后 Linux smoke | NATIVE_VERIFIED，仅合成 ABI | run_arm64_smoke.py v4：runner 0、正常 0、指定退出 23 |
| Bionic clock/pthread/TLS | NATIVE_VERIFIED，仅合成 ABI | linker64 bionic-probe-v2：0 |
| Bionic 库绑定 | NATIVE_VERIFIED，仅本探针 | linker64 --list bionic-probe-v2：0，libc/libdl 指向隔离目录 |
| 版本符号/relocation 静态审计 | IMPLEMENTED | loader_audit.py：0；473 导入中 469 有静态候选 |
| 专项测试 | UNIT_PASS | unittest discover：0，29 项通过 |
| 隔离副本主工程/公开回归 | UNIT_PASS | run_main_engine_checks.py / run_public_checks.py：均 0 |
| 真实 libg 加载 | MISSING_INPUT / NOT_RUN | 锁定 APK 缺失；未取得 LOAD_ONLY |
| 原版生命周期 / no-VM / Android-free | NOT_RUN | 没有以合成 ABI 通过替代 |
| 训练 / 多机放量 / 10× | NOT_RUN | 未启动 |

## 证据与边界

- **PURE_LINUX_SMOKE_PASS**：真实 guest 八项检查、文件 canary、退出码均通过。
- v3 因自有 raw clone 包装器 TLS 参数顺序错误失败；新 v4 独立编译、独立输出。
- Bionic 未传真实库参数，未调用 libg constructors、JNI_OnLoad 或战斗入口。
- 185 个库的静态候选匹配不等于运行时 namespace、间接加载或版本绑定已通过。
- 753 个 init_array 槽尚未执行。4 个 unresolved weak ZSTD trace 符号保持未知。
- 原始日志及二进制保留在私有隔离目录；公开证据只含脱敏命令、汇总、完整 SHA 和退出码。

完整命令、环境身份、输入/输出 SHA、日志名称、失败位置见
[实验报告](PURE_LINUX_GUEST_ABI_2026-10-27.md)和
[证据 JSON](pure-linux-guest-abi-evidence-20261027.json)。历史报告保留，追加续验链接。

## 下一步

取得 SHA `685c115e…bcf2a` 的锁定 APK 并核清历史 manifest 冲突，才开展匹配输入的真实加载。
再解决原生内容上下文及 manager/GameState 的独立初始化对象图，不预先实现完整 framework。
没有修改 main、冻结部署、原版输入或其他运行任务；现有 VM 保持运行，未安装新 VM。

## 发布检查

`python3 tools/check_public_snapshot.py` exit0，2,353 个清单文件核验通过；
`gitleaks dir . --config .gitleaks.toml --redact` exit0，约39.27 MB、零发现；
`git diff --check` exit0。专项复验29项通过。只发布源码和脱敏摘要。
远端任务分支及main的ls-remote核对成功；完整fetch曾因低速超时失败，未修改或合并远端分支。
最终提交SHA和推送结果以交付回执及远端ref核对为准。
