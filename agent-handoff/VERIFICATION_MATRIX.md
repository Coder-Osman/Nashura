# 本任务验证矩阵（2026-10-22）

| 检查 | 成功标准 | 最初状态 |
|---|---|---|
| 公开快照manifest | 更新的源SHA/bytes与新文件一致；独立源目录不污染 | PASS：1984文件；branch 968b143；[CI](https://github.com/Coder-Osman/Nashura/actions/runs/39778686608) |
| 冻结项目CI | 原history-v2原220测试继续通过 | PENDING_CI |
| 当前主工程CI | `firstlight_CR`的Policy/MCTS/FAIR/迁移核心测试通过 | PASS：211项CPU测试；[CI](https://github.com/Coder-Osman/Nashura/actions/runs/39778686608) |
| 新批量Delay | Teacher与Student scalar logit等价，精确联合KL及梯度在固定容差内 | PENDING_CI |
| 解冻Encoder严格路径 | 仍使用逐前缀反传，逐位置KL加法顺序不变 | PENDING_CI + NATIVE |
| 旧batched-v1 CUDA六对 | 已归档独立报告且不改写 | PASSED_OLD_IMPLEMENTATION |
| 旧三局batch256原生恢复 | 累计3次更新严格一致；适用旧身份 | PASSED_OLD_IMPLEMENTATION |
| **新分支真实batch256恢复** | 冻结与解冻阶段不同进程、网络/AdamW/RNG/游标和选优严格一致 | NOT_RUN_ON_NEW_IMPLEMENTATION |
| PUCT首访规则 | 新规则首访最高prior，旧规则可重演，根都128有效预算 | PASS：当前主工程CPU单测；真实实战NOT_RUN |
| PUCT实际同算力胜率 | 独立原生配对局与CPU/GPU总成本报告 | NOT_RUN |
| 根噪声协议 | 训练正噪/评测无噪/无噪消融明确，协议SHA隔离，WAIT质量不变 | PENDING_CI |
| 参数继承覆盖率 | 逐Tensor一致、source/target总数与mapping SHA，Policy/WDL未证者为0 | PASS：共享Parameter别名去重修复后当前主工程CPU单测 |
| 通用卡组FAIR | 固定HOG以外的真实合法历史/原生搜索闭环 | NOT_RUN |
| 正式训练与发布 | 用户另行授权+Gate完整通过 | NOT_STARTED |

**提醒**：只有经过实际测试或凭据审计的项才能从PENDING变成PASS。Codex请新建独立回复文件，不覆盖这张冻结任务表。
