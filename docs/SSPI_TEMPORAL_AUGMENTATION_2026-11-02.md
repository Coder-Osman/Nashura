# SSPI 已执行动作的中间观测增密

日期：2026-11-02。状态：已实现；普通策略的确定性运行时模拟与学习器工程测试，非原生整局、非实战强度结论。运行中的冻结采集部署保持原样，新采集器才启用本功能。

## 行为与训练权重

- 只在实际对局中，为 SEARCH 根选中的 PLAY reservation 采集。普通策略测试直接驱动相同采样器，不运行 MCTS，也不把测试的合成访问数当作真实搜索证据。
- 200 ms 间隔，原延迟 800 ms 得到 +200/+400/+600 ms 三个点；卡牌与格点保持不变，标签延迟为 600/400/200 ms。五档原延迟最多产生 0/1/2/3/4 个点，执行后的状态排除。
- 观察复用 collector 原有逐 tick 的 FAIR view；增加张量化、存储和训练计算，采集端不新增推理、原生 tick、搜索快照或 belief 构造。
- 先处理当 tick settlement，再考虑采样。只有最终 executed 的计划保留；抢占、拒绝、终局前未执行的计划丢弃整组候选。重新生成中间点合法掩码，非法标签跳过。
- 原根 policy 保留完整 MCTS 访问分布。派生 policy 是剩余动作的行为克隆；value 使用实际终局 WDL。没有独立搜索标签，不将这些点计为新增搜索根。
- 用户确认的权重：有 k 个点时，原根 0.5、每个派生点 0.5/k；同时应用于 policy 和 value。没有派生点时原根 1。每个源组总权重恒为 1。
- 派生点嵌入原 TrainingRow，和原根一起划分、采样及消耗曝光次数；128 个源根仍分为 32 个四根微批，辅助样本不额外生成 optimizer step。
- 使用独立的 tensorizer 可变状态及缓存，静态语义表共享。采样不改变 DecisionClock、真实 pending 计划或 actor Tracker。

## 接口与数据兼容

新增 `native_runner/training/sspi/augmentation.py`。collector 默认启用；`--no-temporal-augmentation` 可关闭；experiment 读取 `temporal_augmentation` 配置并将协议和数量写入 checkpoint identity。评估对局不采集派生点。

`episode.pt` 在源根下保存 `continuations`。`episode.json` 保存采样统计及 `actions.json` 校验和。loader 检查 FAIR 摘要、源根、owner、reservation、执行时间/回执、剩余延迟、动作、WDL、数量和合法性；旧 episode 无此字段时沿用原行为。

本次发布同时带入当前唯一 5%×128 采集器与训练/验证分离的前置实现，保留原历史报告，不把历史 1024 根成本当作当前 128 成本。

## 工程验证

定向回归 **66 passed / 14.96 s**，包含真实 FAIR 张量化、7,874,126 参数完整网络的反向与参数更新；原生外部服务未调用。独立发布副本复跑同套件 **66 passed / 20.60 s**；主工程检查 **212 passed**；公开检查九套件合计 **1104 passed / 42 skipped**。源码清单与 gitleaks 检查通过。跳过项主要为需要外部归档/原生附件的历史验证，不视作已通过的原生验收。初次增加真实模型输入测试时，合成 fixture 缺少 action mask 的 exact effective_elixir，结果为 1 failed / 64 passed；补齐 fixture 后单独重跑，失败记录保留在本机隔离证据目录，未降低输入契约。

覆盖五档延迟、普通策略实际提交/执行时钟、抢占当 tick、拒绝、终局失效、非法点跳过、独立 tensorizer、序列化往返、标签与回执错配拒绝、旧数据兼容、0.5/0.5 组权重、共享曝光以及微批累积与整批更新数值一致。

局限：未测新增采集开销百分比；未运行新的完整原生对局或胜率评测。仅筛选 executed 的偏差以及延续标签的质量，仍需后续独立对战实验验证。
