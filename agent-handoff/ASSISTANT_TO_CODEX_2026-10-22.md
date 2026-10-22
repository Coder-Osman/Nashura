# 给 Codex 的交接：当前主工程四项修复与安全集成

**任务来源**：用户明确要求直接实现CI覆盖、条件KL合批及256有效batch恢复验证、PUCT首访同预算消融、权重继承率和根噪审计，并编写实际G2迁移手册。  
**工作分支**：`audit/ci-kl-puct-inheritance-20261022`（不要把冻结生产部署当成该分支）。  
**执行约束**：只在隔离工作副本、独立任务ID与新证据目录操作；任何原生作业均需遵守用户之前G0/G1、磁盘与启动权限；**不要修改当前运行任务，不要启动正式1280局/G2或G3训练**。

## A. 当前主工程CI（已实现代码，需验证）

- 新文件 `.github/workflows/main-engine-ci.yml`、`tools/run_main_engine_checks.py`。
- 以前 `tools/run_public_checks.py` 只测 `_spec_work/versions/fair-final-history-v2/project`（冻结副本），CI即使220 passed也不足以证明 `firstlight_CR` 完全正确。
- 新CI独立运行当前 `firstlight_CR/native_runner/tests` 的核心Policy/MCTS/迁移/FAIR测试；冻结CI继续保留，不合并数量。
- 回执需要提交两组工作流运行链接、main HEAD / branch SHA、测试数量、失败清单、source-manifest校验、源/冻结工程差异清单。对任何没有原生引擎的fixture结果明确标记`CPU_UNIT_ONLY`。

## B. 精确条件KL合批：以既有验证代码为权威

- 主工程新增 `JointPolicy.delay_conditionals_batch`、Teacher `source_policy` 批量Delay条件、Student `ConditionalHeads.batch_delays`；保留完整合法前缀与31槽（0ms非法）。
- **重要**：同仓库已有经CUDA对照的独立 `_spec_work/versions/fair-final-migration-batch-v1/batched_migration.py`、`CONTRACT.md`、`EVIDENCE.md`。其六对CUDA对照、旧梯度求和顺序、AdamW数值容差是**权威比较基线**。与其冲突时不要宣称新代码自然更正确。
- 编码器冻结：允许最多64个位置合批Student Delay，复用Teacher FiLM；编码器解冻：保持旧Student逐前缀反向计算图，仅Teacher按卡缓存、批量计算，并按合法cell顺序逐条累积KL。新代码显式 `batch_delay_enabled` 控制此分支。
- 第一组验证：与全联合枚举的Policy KL、Mode/Card/Cell/Delay条件概率、合法mask及梯度对齐；比较 `source_policy` 的单cell vs batch logits，student有/无冻结encoder。第二组：和独立batch-v1冻结版本6个相同验证帧/同一模型输入逐项对照：loss at/rtol、梯度、AdamW参数、两个moments及RNG。
- **不得**以纯CPU unit test替代真实CUDA逐参数对照，不得仅因KL图变短就修改Loss权重、训练数据或预算。

## C. 有效batch256的真实跨进程恢复

- Codex现有 `fair-final-migration-cache-v1/native-deterministic-resume-verified-20261022.json` 已在**旧工程配置**验证三局1044行、有效batch256、累计3次FP32更新的新进程精确恢复。保留其PASS，不重复说从零未做。
- 但本分支更新了迁移KL计算实现，**旧恢复PASS不能自动证明本分支新实现**。须在独立冻结部署、相同原生数据/教师/优化器/种子下验证 reference连续训练 vs resume从midpoint恢复，至少覆盖冻结/解冻encoder对应阶段。
- 对比网络SHA/所有浮点Tensor、AdamW moments、RNG、数据分割和采样游标、轮次选优历史、Checkpoint及原生输入SHA。恢复自己的轨迹必须逐位一致；与旧计算器的数值对照以原固定容差为准，不能悄悄放宽。
- 原生模型及game数据不可公开：只在工作机本地检验，仓库交付脱敏JSON摘要+验证器/launch源hash；没有真实原生执行结果时标记`NOT_RUN_ON_NEW_IMPLEMENTATION`。

## D. PUCT首次选择与根噪

- `SearchLimits.first_visit_rule='highest_prior'|'legacy_lexical'`，默认新规则；两者都128有效根访问、相同冻结网络/FAIR/对手/候选/同根采样。代码仅对 `N(node)=0` 的第一步改变决策方式，其余PUCT公式不变。
- 原节点旧规则在 `sqrt(N)=0` 时按字典序选边；新规则按最高搜索先验选边。测试必须显式制造两者不同的场景，同时验证128访问、不变先验、根单世界和有效边界不变量。
- **同算力消融仍未完成**：需要在私有原生引擎下对配对种子运行至少两个预注册配置，报告胜率和 CPU/GPU时间/有效标签，而不是把unit-test当棋力证据。
- `SearchLimits.root_noise_mode='training'|'evaluation'|'ablation_no_noise'`：training必须带正噪声，evaluation必须为0，消融禁噪必须显式标记。两者搜索协议SHA不同；保持ACT内加噪及WAIT质量不变。
- 当前训练根是否自动传入`training`不得猜测：检查实际actor调用链，必要时按训练/确认部署入口**显式设置**，不把评测控制器的配置直接复制给self-play actor。

## E. 权重迁移覆盖率审计

- `ConditionalPolicyValue.copy_encoder_with_audit(source)` 与 `inheritance_report(source,mapping)` 返回逐Tensor真实相等验证、源/学生参数元素数、映射数量、复制比例；没有证据的Policy/WDL head必须报告0直接复制。
- 旧A是条件自回归，但新的512维主干与条件head不自动共享相同feature语义；即使shape一致也不得盲目拷贝。应通过继承映射校验、独立模块前向对比，才能扩大映射。
- 把覆盖率、source SHA、adapter SHA、网络结构hash、转换类型和最小完整行动/WDL验收保存在迁移模型的不可变审计摘要，必要时在G2数据入口拒绝缺审计的checkpoint。
- 当前新增的是证据生成接口，未重写旧checkpoint schema或偷偷继承旧optimizer，不得宣称学生“所有自回归头已复制”。

## F. Codex回执标准

回复应新建 `agent-handoff/CODEX_REPLY_2026-10-22.md`，逐项列 `implemented | unit_pass | native_verified | awaiting`，附：
1. Git SHA、冻结部署 SHA、主/冻结工作流URL。
2. 数学精确KL（冻结/解冻Encoder）和AdamW容差数据。
3. 有效batch256同实现跨进程resume的一致性摘要；原生数据和模型文件不要发布。
4. PUCT同预算消融的试验定义与结果（未运行写NOT_RUN）。
5. 权重继承元素数/比例；需要蒸馏的剩余Head。
6. 明确G0/G1、G2–G8仍未闭合的项目，不准升级任何Gate状态。

详见 [G2_MIGRATION_RUNBOOK.md](G2_MIGRATION_RUNBOOK.md)。

## 本分支CI最新证据（2026-10-22）

- branch code commit `968b143029b27bbfbbf0450d48f31c7f38f2d769`: [active firstlight_CR CI](https://github.com/Coder-Osman/Nashura/actions/runs/39778686608) 实测 **211 passed**，公开source-manifest **1984 files_verified**。
- 新增继承率测试首次发现共享Parameter alias去重缺口（210 passed/1 failed），修复于该commit；保留历史失败，不把首次失败抹除。
- 根噪声与PUCT预算为单元/fixture验收；原生新实现的batch256恢复、真实CUDA对照及搜索同算力对局仍按上述列表属于**独立待验收**。
