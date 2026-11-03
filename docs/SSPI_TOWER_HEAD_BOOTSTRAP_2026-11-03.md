# 仅价值头的塔血启发式初始化

协议 `sspi-tower-head-bootstrap.v1`，公式 `sum-hp-difference-over-sum.v1`。
这是独立预热实验，不自动接续旧 SSPI 或 value-only 训练，不做胜率评估。

## 固定模型与数据

- 从 seed=20261103 的全新随机 SSPI WIN/LOSS 模型开始，结构不变。
- 只训练 `Linear(512,256) → SiLU → Linear(256,2)` 的 131,842 个价值头参数。
- 共享主体、策略头及非价值缓冲区冻结，模型保持 eval 模式。
- 新采集 32 局训练、8 局验证、8 局最终留出；三套 game ID/seed 域互不重叠。
- 双方始终使用同一冻结随机策略直出，0 SEARCH；现有每玩家每真实决策独立 50% gate，包含只能 WAIT 的状态。
- 采集器只在已选样本上读取已经冻结的公开 `ObservationV1.towers`，不新增观测、决策或 RNG 调用。
- 复用自然终局准入，平局整局排除、不补采；失败/截断暂停并保存原始证据。
- 保留原 episode 的真实 WL 标签；独立 `tower_hp_target` 和 cache records 存放启发式目标。

## 目标

`v=(H_self-H_enemy)/(H_self+H_enemy)`；`q=[(1+v)/2,(1-v)/2]`，顺序 WIN、LOSS。
H 是国王塔与两座公主塔的实际剩余 HP 直接相加，没有额外权重或奖励。
9000 对 6000 得分 0.2，软目标 [0.6,0.4]。这不是经过校准的真实胜率。

公开 BattleEnv 固定保留六座塔的身份，毁塔记录 HP=0/active=False。
要求每方 king/princess_left/princess_right 完整、无重复、HP 有限且合法；padding 忽略。
缺字段、缺塔、非法值或全零分母隔离，不将未知塔当作已毁。
从公开对象复制原始 HP，避免用经过 float16 压缩的模型输入反算 HP。
每条记录含塔血、公式、软目标、FAIR SHA、owner/tick/decision、采样 seed、动作与实际执行动作。

## 缓存与训练

共享编码器冻结后，逐完整局生成 float32 的 512 维缓存；每局首/中/末样本核对完整前向与缓存前向，容差 1e-6。
缓存是普通无梯度 tensor，可用于价值头反向；包含 source、episode、actions、tensor 和 shared SHA。

损失仅软目标 CE，AdamW lr=1e-3、weight_decay=1e-4、betas=(0.9,0.999)、eps=1e-8；
batch256、clip1、每 epoch 全样本确定性打乱一次，尾批按实际样本数平均。
最多20 epoch，以按局平均验证评分 MSE 选优；改善门槛1e-6、连续3 epoch无改善停止。
初始模型参加选优；共同恢复最佳价值头和对应 AdamW 状态，无改善回滚。
每个已完成 epoch 原子保存完整 progress checkpoint，恢复时从已提交边界继续；中断的未提交 epoch 重算。

最佳完整模型与 optimizer 单独保存。冻结源码、随机初始化、数据和各 checkpoint 均有 SHA。
固定训练/验证状态集核对所有可达条件策略分布，非价值参数/缓冲区 SHA 必须逐位不变。
未来接入 SSPI 时重新建立联合优化器；本轮不接入、不启动后续代际。

额外使用 `python tools/audit_tower_bootstrap.py --run-dir PRIVATE_RUN_DIR --source-root PRIVATE_FROZEN_SOURCE` 对同一训练/验证缓存确定性重放，
包括 `state_dict` 未保存的 nonpersistent 目录缓冲区。逐 epoch 检查全部 `named_buffers()` SHA，
要求重放结果与原始最佳完整模型和 AdamW 逐位相同；不创建新候选、不采新局、不重评留出集。

## 一次性留出与验收

所有采集都使用初始随机策略，选优只读取训练/验证集。
选定 checkpoint 后只计算一次最终留出报告，原子保存；恢复时读取既有报告，不重新选择模型。
报告每局 MSE、MAE、相关性和优势方向准确率，再等权平均；95% CI 按完整对局 bootstrap 2000 次。
相关性在目标或预测方差为0时未定义，报告有效局数；常数基线相关性不定义。
基线为恒零及训练集全部有效状态的平均得分。

固定通过门：

1. 按局平均 MSE 比两常数基线中的较好者至少低50%。
2. 按局平均 MAE≤0.05。
3. |v|≥0.05 的状态按局平均方向准确率≥80%；覆盖至少4局、合计100状态。
4. 冻结、策略保持和 checkpoint/model/AdamW 往返全部通过。

空有效集跳过更新，覆盖不足或拟合失败保存结果，不调整超参、不补采。
通过只说明随机共享表征上的价值头可拟合该启发式；不表示真实胜率学习或60%对战胜率。

## 执行与证据

入口 `python -m native_runner.training.sspi.tower_experiment --run-dir PRIVATE_RUN_DIR --run-id NEW_RUN_ID`。
使用 macOS CPU 与既有 ARM64 Android Emulator、event-json-delta；仅 actual 端点，belief 参数保留接口但不访问。
原生库/probe、完整 episode、缓存和权重留在本机，公开仓库仅发布代码与脱敏结果摘要。
当前阶段验证和实际结果见 `RESULTS.md` 的本实验条目。

本轮已真实完成但未通过拟合门：3 epoch/96步后回滚epoch0，完整结果见 `SSPI_TOWER_HEAD_BOOTSTRAP_RESULTS_2026-11-03.json`。常数相关性的发布勘误没有改变MSE选优、checkpoint或一次性留出验收。

原运行生产源码身份为 `ca101b7eca09c5d8a5120432482b79989a520514a8aef3479103e5bc3c3eff06`。发布版仅在统计报告处修正常数相关性；需要逐字复现原运行时，在独立代码副本应用 `docs/evidence/sspi-tower-bootstrap-20261103/runtime-v1-compat.diff.txt`，再核对上述身份。当前主线保留修正，原始本机冻结部署保持原样。
