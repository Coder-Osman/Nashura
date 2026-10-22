# Nashura

面向实时、部分可观测对战的 FAIR 搜索、自博弈与策略迭代研究工程。

本仓库保存工程源码、冻结规范、独立实现版本与阶段性验证成果。现阶段重点是基础正确性和迁移验收；正式训练尚未启动。

## 当前状态（2026-10-22）

| 阶段 | 状态 | 尚待完成 |
|---|---|---|
| G0/G1：工程与搜索正确性 | 进行中；限定子项登记 34/43 | 完整 Full128 搜索终局、原生补充轮次与选优、一般原生边界等 |
| G2：正式迁移与 WDL 校准 | 未运行 | 正式 1,280 局数据、校准与非劣验收 |
| G3：搜索增益 | 未运行 | 冻结迁移模型的 128 次搜索实战对照 |
| G4–G8：后续训练与发布门 | 未运行 | 依赖前置门闭合 |

测试通过、限定子项通过、整体门通过和模型实战提升分别记录，测试数量存在重叠，不累加。

最近核验：重新fetch全部远端分支，最新主线`8f68763`与本地一致，更新前2010项公开源清单通过。补齐独立预算选优worker和终止内容验收器，保留原冻结部署及完整第7步状态；真实子进程/小模型/交接/父验收联合64项通过184.31秒，集合重叠不累加。真实原生交接和选优尚未执行。快照1792681789：active完成4次冻结更新、2个epoch，reference继续，跨进程恢复未完成；Full128快照1792681423提交96组、192根、24,576次模拟至tick3644，未终局。旧超时失败证据保留；整体门与正式训练状态保持。

## 目录与入口

- [`firstlight_CR/`](firstlight_CR/)：主工程，包含原生适配、策略模型、旧 PPO 基线、FAIR 搜索及测试。
- [`agent-handoff/`](agent-handoff/)：ChatGPT ↔ Codex 专用文字交接：迁移步骤、代码审查、验收证据与回复模板；不属于运行代码。
- [`_spec_work/versions/`](_spec_work/versions/)：隔离的增量实现、回归与证据，包括历史信念、一般退化路径、迁移消费、恢复、CUDA 推理和原生队列审计。
- [`冻结规范`](_spec_work/evidence/fair-final-v1-20261019/spec/clash_royale_fair_final_v1/README.md)：训练语义、数据契约、验收门及实验矩阵。
- [`整体进度`](_spec_work/evidence/fair-final-v1-20261019/DOCUMENT_EXECUTION_PROGRESS.md)与[`闭合清单`](_spec_work/evidence/fair-final-v1-20261019/G0_G1_CLOSURE_CHECKLIST.md)：完整历史记录。
- [`成果索引`](docs/RESULTS.md)、[`复现说明`](docs/REPRODUCING.md)、[`发布边界`](docs/PUBLICATION.md)。

部分历史文档使用本机绝对路径和旧机器部署目录，保留为历史上下文；本仓库入口以相对路径为准。`firstlight_CR` 的历史介绍含旧模型发行说明，本公开快照的实际附件范围以 `publication/source-manifest.json` 为准。

## 快速验证

使用 Python 3.12 或 3.13。先创建独立虚拟环境，安装测试依赖；CPU 版 PyTorch 足以运行下面的工程测试。

```sh
python -m venv .venv
.venv/bin/python -m pip install -r requirements-test.txt
.venv/bin/python tools/check_public_snapshot.py
.venv/bin/python tools/run_public_checks.py
python tools/run_main_engine_checks.py    # 另测当前 firstlight_CR；不替代冻结历史版本
```

原生对局、完整训练及静态二进制审计需要自行准备匹配的游戏引擎、资源及模型。仓库附有适配源码、构建输入源码和身份摘要，第三方引擎二进制、提取资源、私有对局及模型权重单独保留。详见复现说明。

## 目标完成后的更新

每个目标完成后同步相应实现、测试、文档和证据摘要，运行公开检查后提交并推送本仓库。更新流程与凭据处理要求见 [`AGENTS.md`](AGENTS.md) 和 [`维护流程`](docs/MAINTENANCE.md)。长时间运行的任务继续使用其冻结部署，仓库同步独立进行。

沿用原工程的 [Apache-2.0](LICENSE) 与 [NOTICE](NOTICE)；第三方材料另受各自条款约束。
