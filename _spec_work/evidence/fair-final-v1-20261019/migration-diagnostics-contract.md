# 迁移分层诊断工程验收

依据冻结TRAINING_SPEC.md §4.2–4.4及ACCEPTANCE_GATES.md G2A。
本次完成诊断工具及工程验证，未启动正式G2；training_admissible=false。

## 实现与统计口径

- `firstlight_CR/tools/fair_migration_diagnostics.py`计算完整合法条件联合KL、
  教师逐级贪心动作NLL、完整动作及WAIT/ACT匹配、ACT卡牌/位置/延迟匹配。
  位置单分量与卡牌+位置前缀另列，避免跨卡相同坐标被误认为前缀一致。
- WDL使用WIN/DRAW/LOSS顺序，比较teacher KL与P(win)-P(loss)的绝对误差；
  对真实玩家相对终局标签另报CE、三类Brier及预测平局概率。
- 按座位、0–60秒/60–180秒/180秒以后、教师模式/卡牌/位置/延迟分组。
  tick=50ms，边界1200/3600分别进入下一组。每指标报告支持行数、支持局数、
  分子、按行均值与先局内平均再整局等权的均值。
- 教师ACT而学生WAIT时，全部ACT匹配指标计0；教师WAIT不进入ACT分母。
  无支持时使用null，保真检查不判通过。逐级greedy用于保真诊断，
  不等同完整联合argmax，也不代替正式条件随机直出实战。
- `diagnose_validation`只读取validation帧，复核冻结回执、行身份与现场教师
  重算目标。开始和结束核验双方参数/buffer及Python/NumPy/torch CPU/CUDA
  全局RNG摘要不变；局部确定性选择不消耗全局随机流。
- CLI `tools/diagnose_fair_final_migration.py`核对检查点文件SHA及语义摘要、
  来源A、数据/划分、架构、网络形状/dtype/finite值。它只加载网络，
  不恢复优化器/游标/RNG；这不放宽训练恢复的设备拓扑契约。

## 测试与真实子集

专项47项通过（17.03秒），相邻损失/更新器/学习回归64项通过（25.57秒），
两组均零失败、零跳过。分别见migration-diagnostics-tests.xml及
migration-diagnostics-adjacent-tests.xml，不与历史测试数量累加为独立覆盖数。

真实attempt：migration-diagnostics-1792551057836171000，.35，CUDA FP32。
复用native-migration-1792548905478386000的独立工程数据与第一步检查点；
未新建对局引擎、未做训练，退出码0，耗时108.47秒（含加载、索引与校验）。

- 来源A SHA：e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015。
- 工程检查点SHA：bc8fd51cc2f7b483d39597643e2f4883fcb9a0b0875f19a5d0091f4c0131ca5a。
- 验证局358行中确定性抽12行；双方各6行，三个时间段分别3/4/5行。
  教师ACT支持3行、WAIT支持9行；只有1局，按行与整局等权结果相同。

| 指标 | 工程子集结果 | G2A正式要求 |
|---|---:|---:|
| 完整逐级贪心动作匹配 | 1/12（8.33%） | ≥95% |
| 教师ACT完整动作匹配 | 0/3 | ≥90% |
| WAIT/ACT匹配 | 4/12（33.33%） | ≥98% |
| WDL teacher KL | 0.706911 | ≤0.01 |
| WDL标量MAE | 0.787353 | ≤0.02 |
| 学生/教师真实终局CE | 0.953942 / 0.122021 | 另需正式校准证据 |
| 学生/教师真实终局Brier | 0.572424 / 0.057601 | 另需正式校准证据 |

这只是训练1步、batch=1工程权重的只读诊断；没有训练前后或独立实战对照。
指标未达正式要求，不据此宣称正式迁移失败、价值头校准完成或胜率下降。
complete_validation=false；原工程测试局未用于本次诊断。

## 证据与未完成边界

报告保留逐行动作/指标、选择行ID摘要、数据与模型身份、部署源码SHA。
launcher先核验原冻结部署源码与新增tools源码，退出后下载日志及完整报告。
原生迁移生产源码未变；本次来源/测试/原始证据摘要见migration-diagnostics-evidence.json。

该入口当前强制engineering-acceptance，不充当正式选优或测试集验收入口。
仍需正式轮次协调与学生补充采集、验证权重选择/早停接线、真实终局WDL校准、
正式完整验证/最终测试和400/2000局非劣。G0/G1整体及G2状态保持原判定。
