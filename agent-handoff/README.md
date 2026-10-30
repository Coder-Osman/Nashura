# ChatGPT ↔ Codex 文字交接区

本目录用于跨助手、跨Codex任务共享**文字任务与证据**，不属于游戏引擎、Replay、权重或部署代码。与现有 `AGENTS.md`、冻结规范同级协作；不能替代它们。所有记录应注明日期、commit SHA、版本与证据状态。

- [SERIAL_ARM64_COMBINED_PROFILE_2026-10-31.md](SERIAL_ARM64_COMBINED_PROFILE_2026-10-31.md)：组合候选四组 warm-step 软件采样、精确函数范围及下一热点优先级。
- [SERIAL_ARM64_COMBINED_FOLLOWUP_2026-10-31.md](SERIAL_ARM64_COMBINED_FOLLOWUP_2026-10-31.md)：排除本轮工程检查重叠的 32 批独立复测、八个全帧门及 49536 局有界同进程稳定性。
- [SERIAL_ARM64_HOT_DISTANCE_2026-10-31.md](SERIAL_ARM64_HOT_DISTANCE_2026-10-31.md)：旧 getter 与饱和距离显式组合、16 个完整局全帧门及 64 批平衡顺序四路径增量验证。
- [SERIAL_ARM64_SATURATED_DISTANCE_2026-10-31.md](SERIAL_ARM64_SATURATED_DISTANCE_2026-10-31.md)：整数饱和距离指令对照、八个完整局门及四组交错串行收益。
- [SERIAL_ARM64_THREAD_RESOURCES_2026-10-31.md](SERIAL_ARM64_THREAD_RESOURCES_2026-10-31.md)：线程资源边界、硬件 PMU 能力与 41280 局有界同进程串行稳定性。
- [SERIAL_ARM64_COMPONENT_GETTERS_2026-10-31.md](SERIAL_ARM64_COMPONENT_GETTERS_2026-10-31.md)：对象临时分配/组件归因、16 完整局全帧复核、72 批三路径串行测量及未证实的增量收益。
- [SERIAL_ARM64_NATIVE_PHASES_2026-10-31.md](SERIAL_ARM64_NATIVE_PHASES_2026-10-31.md)：四组原始/诊断/inject 全帧复验、真实 command dispatch 与对象集合更新归因。
- [SERIAL_ARM64_WARM_WINDOW_2026-10-31.md](SERIAL_ARM64_WARM_WINDOW_2026-10-31.md)：仅 warm-step 窗口的 CPU 归因、长串行 task 等待修复与同构建重复基线。
- [SERIAL_ARM64_SAMPLE_PHASE_2026-10-31.md](SERIAL_ARM64_SAMPLE_PHASE_2026-10-31.md)：串行 warm-step 采样相位复核及旧基线可比性边界。

- [SERIAL_ARM64_HOT_GETTERS_2026-10-31.md](SERIAL_ARM64_HOT_GETTERS_2026-10-31.md)：四组完整局全帧回归、热 getter 内联和 ABBA/BAAB 串行收益及本机 QEMU 边界。
- [SERIAL_ARM64_SPATIAL_Y_BOUND_2026-10-31.md](SERIAL_ARM64_SPATIAL_Y_BOUND_2026-10-31.md)：多卡组固定动作、空间查询候选及未证实加速的完整结果。

## 文件约定

- [ASSISTANT_TO_CODEX_2026-10-22.md](ASSISTANT_TO_CODEX_2026-10-22.md)：此次修复与合并工作的明确请求，包含四项改动和不允许做的事。
- [G2_MIGRATION_RUNBOOK.md](G2_MIGRATION_RUNBOOK.md)：从原A正式权重迁移到新的条件自回归网络的执行说明。
- [VERIFICATION_MATRIX.md](VERIFICATION_MATRIX.md)：GitHub CI、冻结CUDA对照、256有效batch恢复、同算力首访消融、权重继承率与根噪审计。
- [CODEX_REPLY_TEMPLATE.md](CODEX_REPLY_TEMPLATE.md)：Codex逐项反馈格式。

## 沟通协议

1. ChatGPT只更新本任务的 `ASSISTANT_TO_CODEX_*.md` 或新增独立日期文件；Codex在 `CODEX_REPLY_YYYY-MM-DD.md` 新建回执，不覆盖请求历史。二者均通过GitHub提交记录及 SHA 交接；**不要同时覆盖同一文件**。
2. 所有“通过”必须指向命令、原始回执、报告及SHA；如果依赖私有原生引擎/权重，在公开仓库只放已审计的摘要和哈希，不提交数据/模型。
3. 在不同分支工作时，标记明确来源和目标；不得将运行中的冻结部署改成当前工作树，不能因合并/修复而改变正在运行的原生搜索或迁移任务。
4. 先在独立分支通过公开CI，检查base main在合并期间是否有新提交；满足所有正确性门才由维护者按工程规则合并。不强推、不更改Champion、不启动正式训练。
5. 当前G0/G1仍未整体闭合，G2–G8不能因为本文件或pytest通过而登记PASS。

## 最新任务

2026-10-22：`audit/ci-kl-puct-inheritance-20261022`。回复时填写模板，特别报告与 `fair-final-migration-batch-v1` 数值对照的差异，和是否完成**新合并实现**的batch256跨进程恢复（不能借旧版本通过直接替代）。
