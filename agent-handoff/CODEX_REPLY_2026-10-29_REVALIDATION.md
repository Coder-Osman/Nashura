# 2026-10-29：worker2 后续复验

本轮接续 `worker2` 的 holder setup 边界工作，基线为
`1417278 Record Linux native holder setup boundary`，运行目录为公开 Nashura
仓库。复验只覆盖工程测试和环境状态，不把工程测试当作 Linux 原生完整局。

## 工程门

使用 `/opt/anaconda3/bin/python3`（Python 3.13.5）运行：

| 检查 | 结果 |
|---|---|
| `tools/pure_linux` | `1105 passed, 1 skipped` |
| `tools/run_public_checks.py` 主公开组 | `794 passed, 27 skipped` |
| affordability | `23 passed` |
| selection timeout | `82 passed` |
| position guards | `15 passed, 3 skipped` |
| live command | `25 passed, 4 skipped` |
| dispatch command | `31 passed, 3 skipped` |
| reservation mapping | `31 passed, 2 skipped` |
| command coordinates | `38 passed, 3 skipped` |
| collection coordinator | `65 passed` |
| `tools/run_main_engine_checks.py` | `211 passed` |
| `tools/check_public_snapshot.py` | `passed`, 2778 个源文件 |
| `gitleaks dir . --config .gitleaks.toml --redact` | `no leaks found` |

这些结果只更新公开工程检查的复验时间和 XML 元数据。Python 3.9 直接运行公开
脚本时收集阶段出现20项环境错误，涉及 `StrEnum`、`dataclass(slots=True)`、
`typing.Self` 及缺少 `lief`/`paramiko`；该尝试不计入产品失败，也没有用它覆盖
Python 3.13 的通过结果。

## Linux 环境门

VMware Fusion 仍列出同一 Ubuntu 22.04.5 ARM64 VM；`192.168.127.128:22` 仍返回
`Connection refused`。没有有效 guest 登录通道，因此没有执行
`provider_job.py --holder-setup-only`，也没有生成新的 `result.json`、
`stdout.log` 或 `stderr.log`。

| 项目 | 状态 |
|---|---|
| Android holder setup | `PASS`（沿用已审计证据） |
| Linux holder setup | `BLOCKED_ENVIRONMENT / NOT_RUN` |
| `CATALOG_READY_PASS` | `false` |
| ResourceManager、GameState、Step、完整对局、性能 | `NOT_RUN` |

下一步仍需在不改变冻结部署的前提下取得 guest 登录通道，然后按
`PURE_LINUX_HOLDER_SETUP_2026-10-29.md` 的相同版本运行 holder setup probe 并审计
三份日志。工程测试通过不解除 Linux 原生门。
