# 全历史数据消费与补充训练工程接口

依据冻结 TRAINING_SPEC.md §4.3–4.4。独立于生产入口及运行中的244文件身份。
原 migration_series.py 与其报告保持原身份；新增 series_training.py 复用其强制预检。

## 数据消费

SeriesMigrationDataset 先审计所有轮次，再索引每个原始整局；保留各轮 manifest、
回执和原始划分，绝不拼造单轮 manifest。允许补充轮工程数据仅含 train，
但整个历史必须有非空 train/validation/test。正式配额由后续协调器另外验收。
首轮数据身份与原 MigrationDataset 完全兼容；两轮及三轮身份绑定全历史
series SHA、回执和私有审计摘要。私有种子仅用于预检，不进入模型 batch。
每批重新核验原回执、FAIR语义、合法性，并实时重算冻结A动作/WDL核对记录。
新接口已强制执行全历史预检，但旧采集入口及正式协调器尚未接入。

## 训练与恢复

首轮复用原 MigrationTrainer。SupplementTrainer 从完整父训练 checkpoint 开始，
要求新轮行为模型 SHA 等于父文件 SHA，父历史恰好是当前历史前缀且回执不变。
继承父模型与 AdamW moments，保留累计优化步数，重新冻结本轮独立采样顺序。
编码器从本轮开始参与更新，最多5个完整 epoch；末批可不足 batch。
复用原损失、微批累积、FP32、AdamW基线、梯度裁剪，不调整损失权重。

新 checkpoint 类型 FairFinalSupplementTrainer/v1 保存全历史身份、父文件/数据/
模型SHA、累计父步数、当前轮、模型、优化器、全局RNG及采样游标。
恢复检查文件和语义checksum、实现身份、模型结构/形状/精度、AdamW moments、
累计步数与本轮样本计数、洗牌RNG和五轮上限；失败恢复原全局RNG。
新实现不通过修改旧checkpoint日程字段来复用原12轮恢复器。

当前工程入口只接收已完成固定日程的父训练。验证集选权重、早停父模型的
选择凭据及恢复、正式预算协调仍需独立实现；这里不代替G2迁移流程。
保存的父模型SHA用于来源追踪；祖先文件真实性仍须结合原始SHA与原生验收。

## 证据边界

真实小模型、完整fixture整局上的梯度、两轮连续补充与精确恢复测试，是工程
正确性证据。既有原生整局可用于独立只读索引/抽样目标重算，但没有新原生
round2局，也没有正式1280局、选权重/早停、WDL校准或非劣实战结果。
所有工程数据 training_admissible=false，formal_g2_pass=false；不自动进入Replay。

probe_series_dataset.py 接收 --source、--private-plan、--data-roots、--output，
每个完整局抽一行实时核验A目标，保留原始文件mtime/SHA，报告不含训练更新。
只对已抽查行声明重算通过，不把抽查推广为全1310行目标核验。
