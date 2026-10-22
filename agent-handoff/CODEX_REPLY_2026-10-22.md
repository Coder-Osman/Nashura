# 最新审计实现整合与兼容性回执（2026-10-22）

本次目标是将 GitHub 最新实现整合到本机，并在保留已有成果和冻结部署的基础上继续工程验收。不是正式迁移或训练启动回执。

## 版本与源码

- 同步时核对全部远端分支及 HEAD：`main` 为 `5956468eabae167a71e317d7842543980d54a9eb`，最新审计分支 `audit/ci-kl-puct-inheritance-20261022` 为 `8195a5a07b8bf7d9533fc7a3bfbe83b0c5083a99`。
- 本机 main 已 fast-forward 整合该分支的五个提交；本机未发布的归档验收器和多轮数据身份修复已恢复，保留同步前 stash 备份。
- 核对并同步八个 active 源文件到仓库外源工作区，再导出快照，避免旧源目录覆盖新实现。逐文件 SHA 见 `INTEGRATION_2026-10-22.json`。
- 没有改动正在运行的原生冻结部署；history-v2 仍为独立参考版本。八文件同步不代表所有 active 与历史版本相同。

## 已执行验证

| 范围 | 命令 | 结果 |
|---|---|---|
| 当前主工程 CPU | `python tools/run_main_engine_checks.py` | 211 passed，24.12 秒 |
| 冻结 history-v2 CPU | `python tools/run_public_checks.py` | 220 passed，39.00 秒 |
| 归档验收器与旧 batch-v1 在新 active 源码下的兼容性 | `pytest test_selected_collection_verifier.py test_pipeline_verifier.py test_batched_migration.py`，测试目录见 JSON | 75 passed、3 skipped，69.40 秒 |
| 导出文件身份 | `python tools/check_public_snapshot.py` | 1,990 个文件通过 |

兼容性测试的三个跳过项因缺少私有冻结部署附件（打包及篡改检查）而跳过，不计为通过。上述结果均属于 CPU 工程检查；fixture 不作为完整原生采集、CUDA 数值或模型强度证据。

所拉取提交的 GitHub CI 已成功：

- [当前主工程](https://github.com/Coder-Osman/Nashura/actions/runs/39779254288)
- [冻结参考](https://github.com/Coder-Osman/Nashura/actions/runs/39779254266)

## CUDA、恢复与搜索边界

- 旧 batch-v1 六对 CUDA 对照与旧三局 / 1,044 行 / 有效 batch256 / 三次更新的跨进程恢复保留原范围 PASS。旧报告的实现身份、数值容差和适用范围保持原样。
- 最新实现的冻结 / 解冻 Encoder CUDA 损失、梯度、AdamW 对照：`NOT_RUN_ON_NEW_IMPLEMENTATION`。CPU 条件 KL 测试不代替此项。
- 最新实现的有效 batch256 连续更新对比新进程中点恢复，含网络、AdamW、RNG、采样游标和选优一致性：`NOT_RUN_ON_NEW_IMPLEMENTATION`。
- PUCT highest-prior 首访、legacy 字典序及 128 有效访问有当前工程单测；真实同预算配对对战及算力成本对照 `NOT_RUN`。
- `training / evaluation / ablation_no_noise` 已有显式协议及 CPU 检查。新版 probe 使用默认 evaluation；尚未验证正式 actor 到 training 正根噪的端到端贯通。旧训练模块不充当该证据。
- 参数继承审计及共享 Parameter 别名去重的 CPU 检查通过；真实源 A bundle 的继承元素数及覆盖比例尚未重新计算，不能引用 fixture 比例。Policy / WDL 直接继承仍无证据，按零复制处理。
- 通用卡组真实合法历史和原生搜索闭环仍 `NOT_RUN`。

## 运行中任务与准入

最后运行快照为 Unix 时间 `1792673315`：原冻结 Full128 局累计提交 89 组、178 根、22,784 次有效模拟；tick3412，尚未原生完整终局。旧 batch-v1 五阶段工程链路仍处于选优阶段；这些快照不是终局或新实现验收结果。

G0/G1 继续进行中，限定子项保持 34/43；G2–G8 未运行，正式训练未启动。中心 200 GiB 磁盘门不降低；归档内容验收器的 training_admissible、formal_g2_pass、gates_closed 继续为 false。

## 下一步

保持原局及冻结部署原样，继续取得完整终局与恢复证据；最新实现在独立部署身份下重做 CUDA 和 batch256 恢复对照。G0/G1、磁盘门及正式阶段授权满足后，再执行 G2 / G3；本次代码整合不直接升级任何整体门。
