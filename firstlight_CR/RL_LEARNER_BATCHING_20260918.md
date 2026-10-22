# 学习器合批、输入与 value 复用

2026-09-18 实施。原正式 30 轮训练已完成至 201，本次没有追加正式训练轮次。新学习器已写入本地源码，并部署到 `.34` 的独立 `learner-batched-code` 快照；正在运行的评估仍使用原代码。

## 实现

- `native_runner/training/event_batches.py`：本轮观测一次打包，按独立决策行合批；不同温度、不同 optimizer group 分开。每个 batch 保留原 lane/row 身份。
- `event_preflight.py`：全量行为概率校验使用同一份输入，校验成功后保留 detached value，异常诊断仍指向原对局和原决策。
- `event_ppo.py`：GAE 仍逐轨迹计算，复用 preflight value；梯度按有效行数加权，optimizer.step 次数保持原配置。更新后 KL 重新编码已更新模型，复用输入但不复用旧输出。统计量在设备端累计后集中回传。
- `tools/event_rl_update.py`：默认 `--row-batch-size 128 --input-cache-mib 512`。`--row-batch-size 0` 使用逐轨迹分块路径。缓存仅涵盖输入，不保存反向传播激活图。

模型参数、buffer 或相关模块配置变化后，value 缓存失效；critic warmup 显式清空缓存。含 dropout 或 BatchNorm 等训练/评估差异的模块不复用 eval value。行为张量变化、lane 更换或 optimizer 分组改变会拒绝复用缓存。

512 MiB 是观测输入在设备上的缓存预算，不是整个训练显存上限。超出预算的 batch 保留 CPU 打包结果，使用时传入设备。原始观测在本轮内按不可变数据使用。

## 完整历史一轮 A/B

相同第 191 轮 checkpoint、相同 AdamW moments、第 192 轮全部 108 个 shard / 216 条轨迹 / 3,970 条决策；在 `.34` 的 cuda:1 上顺序比较原生产实现和新版。PyTorch 2.5.1+cu121、FP32，每种均一次 optimizer.step。模型与轨迹加载时间单独计量，核心计时包含梯度捕获的验证开销。

| 路径 | 打包（秒） | preflight（秒） | PPO 含更新后 KL（秒） | 核心合计（秒） | 峰值显存（MB） |
|---|---:|---:|---:|---:|---:|
| 原生产实现 | — | 16.04 | 68.12 | **84.17** | 1,078 |
| 新版 128 条 | 1.98 | 2.32 | 9.47 | **13.78** | 3,573 |
| 新版 64 条 | 2.26 | 3.60 | 15.33 | **21.20** | 2,610 |

选择 128 条作为默认值：该真实数据集核心耗时减少约 **83.6%**，吞吐约 **6.1 倍**。每遍由 220 个小 batch 减至 32 个，并省掉 GAE value 的重复编码。实际输入缓存为 420,138,898 字节，全部落在 512 MiB 预算内。

该结果不是整轮训练的 6.1 倍：采集、模型加载、轨迹读取、外部 checkpoint 校验、归档等仍有成本。此次相同数据集的共享模型加载 15.84 秒、轨迹读取 8.05 秒。真实 CLI 新进程从启动到更新保存完成约 49.23 秒；此项未包含外部回读校验和归档。

## 一致性

128 条版本相对原生产实现：

| 对比项 | 最大绝对差异 |
|---|---:|
| preflight value | 8.94e-7 |
| GAE advantage | 9.54e-7 |
| GAE return | 4.77e-7 |
| 梯度 | 9.41e-8 |
| 一次 AdamW 更新后参数 | **4.47e-8** |
| 更新后全体动作 log-prob | 1.05e-5 |

梯度相对 L2 差异为 1.87e-6。原 / 新更新后 KL 为 `0.0002144710003932` / `0.0002144714573497`，相差约 4.57e-10。所有 preflight 通过；浮点归约顺序不同，因此不要求逐位相同。

64 条版本也通过相同校验。零设备缓存预算的 CUDA 测试验证了 CPU 缓存回退、动作概率、value 复用和完整更新路径。

本地相关回归 78 项通过，另补充合批路径 KL 提前停止测试 1 项通过。覆盖不同轨迹长度与 padding、温度边界、多 optimizer group、多 epoch、BC/reference KL、梯度与 AdamW、缓存失效、异常行定位、优化器续训及行为身份校验。

远端相同范围共 79 项通过（59 + 20）。实际 CLI 保存后已通过 strict load 回读：父权重身份、契约、无状态配置及有限参数/优化器状态全部符合预期，迭代与 optimizer_steps 均为 192。

## 远端接入与边界

新版快照：

`/home/gujiga/firstlight_CR/runs/event-residual-v3-30r-20260918/learner-batched-code`

使用该目录下的 `.venv/bin/python tools/event_rl_update.py` 即默认启用合批与缓存；下一次训练应选择该学习器入口，或从已更新的本地源码创建新快照。采集和评估无需改变。

原运行的 `code` 保持原 hash。新版快照以原生产快照为底，仅覆盖本次学习器相关改动；本地已有但尚未部署的法术约束代码没有随本次快照一起部署。

CLI 历史验证产物位于 `optimization-audit/batched-cli-smoke.pt`，它用于检查保存/回读链路，不是正式第 202 轮，也不会接入当前评估。正式最终权重仍为第 201 轮。

## 证据

本地 `evaluations/performance-audit-20260918/`：

- `batched-learner-validation.json`：原版、128、64 的完整计时与数值比较。
- `batched-cli-smoke-status.json`、`batched-cli-checkpoint-verified.json`：真实 CLI 保存与回读验证。
- `cache-overflow.json`：零设备缓存预算测试。
- `learner-batched-manifest.json`：远端独立快照的本次文件 hash。
- `validate-batched-learner.py`：完整 A/B 脚本。
- `batched-learner-tests.log`、`batched-learner-extra-tests.log`、`local-batched-tests.log`：回归记录。
