# 第 171 轮迁移至无状态门控 + 残差 MLP，续训 30 轮

最终结果：已完成至第 201 轮。真实引擎对原始第 171 轮评估 2,160 局，1,150 胜、0 平、1,010 负，胜率 **53.24%**，配对 95% 区间 **51.25%–55.28%**。见 [完整评估](evaluations/final-201-vs-171-20260918/report.md)。

用户已选择：保留无状态门控计算，增加零初始化残差 MLP；迁移验证后先续训 30 轮，并评估相对原始第 171 轮的提升。

## 架构与迁移

- 2 个残差块，每块 `h + Linear(SiLU(Linear(LayerNorm(h))))`，输入、隐藏、输出宽度均为 512。末层 weight/bias 为零，其他层正常初始化，保证起点为恒等映射且之后有梯度。
- 保留 LSTM 零状态下的 input/candidate/output 门，移除 hidden-to-hidden 权重及 forget gate；新模块忽略传入的跨步状态。
- 移除未使用的旧 V4 动作解码器；保留实体/关系 Transformer、空间编码器、公开推算特征与事件动作头。
- 上一动作仍为空。本次训练版保留该恒定分支和原输入投影的参数化，避免折叠后伪造 AdamW 动量。部署推理时可另行折叠。
- 参数数：原 13,046,380 → 新 **9,953,219**。新增残差块合计 1,052,672 参数。
- 旧参数的 AdamW 状态按名称对应，门控张量及动量按 input/candidate/output 行切片；保留 347 项状态。12 个新残差参数张量从新 AdamW 状态开始。累计训练计数仍为 171 轮 / 171 更新步，不把迁移算作训练。
- checkpoint 使用显式 v3 架构契约；旧 v2 loader 路径和契约 hash 保持兼容。不同架构对战仅在明确允许、输入/动作语义和观测配置一致时启用。

原始 171 权重 SHA256：`ebd078dd6e1cfe22671c5dd43bf4d04ee135a1d3af5050d6c73f70676c56ece1`。

迁移权重 SHA256：`9eb059a9ae2e5b232036cd515f59f59fad6be75ee753e4960317420ab177136e`。

新架构契约 SHA256：`0002b04a924d82c228102126b7631eb53d6a80bda670a54d0663683756db0cb3`。

## 已完成验证

本地 71 项模型/checkpoint/PPO 测试及 20 项优化器恢复、续训缓存、评估统计测试通过；远端运行快照中的 71 项测试也通过。测试包含零初始化时的原函数等价、输入跨步状态不影响输出、残差新分支可以学习、AdamW 行切片正确、新旧 checkpoint 严格保存/加载。

原始第 171 轮真实数据中选取 94 条决策（62 ACT / 32 WAIT），经过新格式保存重载后，与原模型比较：策略上下文、value、四头 logits、stored-action log-prob 最大绝对差均为 **0**。这是抽样数值证据，不把它当作所有输入与后续训练轨迹都等价的证明。

真实对局 smoke 已完成：2 局自博弈得到 88 条完整决策，PPO preflight 最大 log-prob 差 4.29e-6，隔离更新后 KL=0.000981。两个残差块的末层均从全零变为非零，最大权重变化约 1e-5，证明分支确实学习。更新后计数为 172 轮 / 172 步，原迁移起点 hash 不变。随后迁移起点与原始 171 的一对换边局均正常终局、零非法动作，跨架构评估通路通过。smoke 权重不作为正式起点。

本地证据：[迁移验证](evaluations/residual-migration-20260918/verification.json)、[真实对局与更新 smoke](evaluations/residual-migration-20260918/smoke-success.json)。

## 训练与后评估配置

- 新任务名：`event-residual-v3-30r-20260918`。
- 学习机 `.34` 根目录：`/home/gujiga/firstlight_CR/runs/event-residual-v3-30r-20260918`。
- `.35/.37/.39/.41` 根目录同名，用户名为 `zhichun`；共 108 路原生引擎。
- 从迁移后的 171 起点继续，第 172–201 轮，共 30 轮；每轮 108 局、216 条双方完整轨迹。
- PPO 保持 1 epoch、全批一次更新、lr=1e-5、target KL=0.01、entropy coefficient=0.01、温度 1、margin beta=0.05、BC/reference KL=0。本次不同时改 epochs、奖励或历史对手比例。
- 最大推进步长统一为 30 tick；重试保留原 seed、checkpoint 与温度，最多三次，持续失败使整轮停止；每机独立保存 barrier 和所有失败尝试。
- 训练 seed 基数 1880918000，按轮次/主机/端口派生；smoke seed 使用单独范围。
- 权重逐轮归档至 `.37` 并验证 SHA256，`.39` 仅清理由学习机保有验证副本的本次权重缓存，保留最近两份；`.34` 沿用已验证的归档后缓存空间保护。
- 30 轮完成后，常驻 pipeline 自动启动 **201 对原始 171 的 2,160 局评估**：1,080 个新种子、每种子换边，11 级石头人镜像、四头温度 1、margin beta=0、30 tick。固定跑满，按种子配对 bootstrap 计算 95% 区间。
- 此比较衡量“架构迁移 + 30 轮续训”的总体效果；没有额外启动相同 30 轮预算的旧架构对照，故提升不应单独归因于残差块。

## 状态与文件

正式 pipeline 已于北京时间 2026-09-18 11:14:43 启动，PID 为 `256632`。截至 11:20，第 172 轮已成功更新并归档，正在采集第 173 轮；本次 30 轮预算已完成 1 轮，训练尚在进行。

第 172 轮共 108 局、216 条完整轨迹、3,938 条决策，没有不完整、截断或被拒绝的轨迹。更新前 log-prob 最大差 `1.0967e-5`，校验通过；正式更新 1 步，更新后 KL=`0.000386`，低于目标 `0.01`。新权重 SHA256 为 `a743023cf92bd7b3f138b4ac1586dc7ad99334d2b0eb378526f4137ea0e87034`，已归档至 `.37` 并验证 SHA256。首轮状态证据保存在 [first-formal-iteration.json](evaluations/residual-migration-20260918/first-formal-iteration.json)。

新目录内：`prepare-status.json`、`migration/verification.json`、`smoke-status.json`、`smoke-success.json`、`pipeline-status.json`、`status.json`、`training.log`、`iteration-*/update.log`、`iteration-*/host-*-barrier.json`、`post-training-eval/status.json`、`post-training-eval/report.json`。

关闭客户端后远端进程继续；没有新建定时任务或应用心跳。30 轮和后评估由本次常驻 pipeline 顺序执行。

## 采集器升级（2026-09-18 13:03）

第 191 轮完成后，在完整轮次边界切换到常驻 Python 采集器，从第 192 轮恢复。新 pipeline PID `317881`，日志 `training-persistent.log` 和 `pipeline-persistent.log`；原 `training.log` 保存至第 191 轮。总目标仍为 201，随后自动进行原定 2,160 局评估。

升级包含未就绪窗口跳过推理、每端口常驻 worker、按机一次回传与逐文件哈希验证、约 40 MB 纯推理权重分发。训练用完整 checkpoint、AdamW、奖励、30 tick 推进和严格 on-policy barrier 保留。新采样协议在加载权重之后设 seed；冷/热启动采用一致 priming，并验证了同 seed 重复对局记录一致。

第 192 轮首轮采集/汇总约 132.7 秒，108 局全部成功，216 条完整轨迹、3,970 条决策；PPO preflight 最大 log-prob 差 `9.54e-6`，通过。首轮含常驻进程初始化，热复用性能另行记录。

截至 13:13，第 192/193 轮均已更新并归档，第 194 轮正在运行。第 193 轮所有 108 个 worker 复用上一轮 PID，换权重平均约 0.4 秒，零重试。两轮分别耗时 272.43/272.88 秒，相比切换前最后 8 轮平均 310.07 秒，初步缩短约 12%；后评估尚未开始。

详见 [采集器优化记录](RL_COLLECTOR_OPTIMIZATION_20260918.md) 和 [验证证据](evaluations/residual-migration-20260918/collector-upgrade-validation.json)。
