# PUCT 树搜 RL 源码接管包清单

更新时间：2026-10-01

## 归档内容

- `research/puct/`：从 `artifacts/hog-tree-search-1755-200r-20260929/` 抽取的顶层 Python 源码和测试，覆盖树搜、原生适配、事件 rollout、教师目标、教师训练、formal 调度和 fleet 预检。
- `native_runner/`：PUCT 运行所需的原生环境、V1 contracts、事件训练模块、V4 模型输入/策略依赖、probe 源码和相关脚本。
- `PROJECT_HANDOFF.zh-CN.md`：当前树搜 RL 的架构、数据边界、接口、远端合同、性能和验证说明。
- `EVENT_DRIVEN_SPEC.md`：作为事件运行时接口参考，接管主线仍以本目录文档为准。
- `requirements.txt`、`requirements-render-web.txt`、`pyproject.toml` 和配置模板：用于重建源码检查环境。

## 明确排除

| 路径/模式 | 原因 |
| --- | --- |
| `artifacts/**`（PUCT 顶层 `.py` 白名单除外） | 历史运行目录、对局、日志、状态和大产出 |
| `checkpoints/**` | checkpoint 和 optimizer 权重 |
| `evaluations/**` | 评测矩阵、逐局证据和报告产物 |
| `.venv-live/**`、`**/__pycache__/**`、`.pytest_cache/**` | 本机环境和缓存 |
| `*.pt`、`*.pth`、`*.safetensors`、`*.zst`、`*.parquet`、`*.arrow` | 权重、压缩数据和回放数据 |
| `*.apk`、`*.aab`、`*.so`、`*.img`、`*.qcow2`、`*.avd`、`*.mp4`、`*.mov` | 原生二进制、guest 和媒体产物 |
| `*.png`、`*.jpg`、`*.jpeg`、`*.gif`、`*.ico` | 非树搜 RL 的媒体资产 |
| `*.log`、`*.pid`、`**/runs/**`、`**/cache/**`、`**/records/**` | 运行态文件 |
| `.env`、`.env.render-web`、`MACHINES.md` | 真实密码、主机信息、用户和 SSH 认证材料 |
| PUCT `config.json`、`match-plan.json` 及 JSON 结果 | 绑定具体权重、端口和本次实验运行状态；字段和固定合同已转述到文档 |

## PUCT 源码白名单

归档脚本只从 PUCT 实验目录复制 `*.py` 和必要的 `README.md`，目标目录为 `research/puct/`。不复制该实验的 `formal/`、`logs/`、`runtime/`、`training-check/`、`avd-flat/`、JSON 结果、权重和引擎目录。

## 凭据和远端信息

源码中的 fleet 调度接口保留，但真实 inventory、SSH agent、用户名、密码、密钥、主机 IP 和运行根目录由部署环境提供。接管文档使用 `HOST_A`、`TRAINER_HOST` 等角色名，不把本机 `MACHINES.md` 或真实 `.env` 放入归档。

## 解包后验证

```bash
tar -tzf firstlight_CR_puct_rl_handoff_20261001.tar.gz
python -m compileall research/puct native_runner
python -m pytest research/puct/test_tree_search.py research/puct/test_teacher_targets.py
```

归档大小应保持在源码级别；若内容出现权重、日志、对局 JSONL、APK、AVD 或大型媒体，视为打包失败。

## 复现前置条件

源码包不包含完整对局运行所需的 checkpoint、原生引擎、用户提供 APK、replay 数据、远端机器和认证材料。正式 PUCT 训练需要按接管文档重新注入这些外部依赖，并始终在远端运行采集进程。
