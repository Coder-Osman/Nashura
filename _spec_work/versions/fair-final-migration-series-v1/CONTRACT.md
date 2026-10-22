# 全历史迁移轮次预检 v1

独立工程工具，不修改主项目、history-v2或运行中完整搜索局的冻结源码。
依据冻结TRAINING_SPEC.md §4.3及ACCEPTANCE_GATES.md G1/G2A。

## 解决的缺口

原补充轮入口只检查当前轮与相邻父轮。此工具接受从round0开始的完整历史，
统一检查来源/协议/namespace/卡组、所有历史任务与pair ID、完整换边座位、
整局划分及实际引擎seed。相同seed仅允许属于同轮、同pair、同划分；
同pair的换边局必须使用同一引擎seed。执行RNG独立登记，保留实际采集值，
不以执行seed相等推断两种RNG共享。
本工具的引擎seed只接受规范化unsigned 32-bit表示，拒绝负数、布尔、浮点及溢出；
不把不同整数经原生截断后相同的比赛误认为独立种子。未改变旧引擎入口取值范围。

私有计划结构为schema=FairFinalMigrationSeries/v1、rounds、excluded_engine_seeds。
rounds中每项为manifest与private_tasks；后者按match ID记录engine_seed和execution_seed。
私有计划只供协调/审计使用，不进入FAIR观察、教师模型、搜索器或训练batch。
excluded_engine_seeds只覆盖调用者明确提供的外部seed集合；空集合不证明历史全局互斥。

可在采集前预检已冻结轮次；后续学生权重尚未产生时，不伪造未来行为SHA。
加入补充轮时必须重验从round0开始的全部历史。--require-final-round要求三轮齐全。
这是独立调用的工程预检，尚未接入正式调度器，不代表旧入口自动获得保护。

## 已完成局审计

--data-roots逐轮只读核对原manifest、完整回执集合、解压数据校验和、生命周期、
双方终局标签、行ID唯一性，以及成功attempt私有种子与来源/行为模型。
输出series SHA绑定计划、各原始回执及私有审计文件，原manifest与原数据均保留。
重复运行输出一致；已有输出身份冲突直接拒绝，不覆盖报告。

此审计不重算教师目标、不重新证明原生来源或私有审计真实性，须结合各轮既有
原生验收使用。也不加载checkpoint证明父训练集身份，不提供合并训练dataset、
补充训练恢复或正式配额协调。工程数据training_admissible=false、formal_g2_pass=false。
round2测试fixture不能替代原生round2。G0/G1整体及G2保持原状态。

运行方式：使用firstlight_CR/.venv-live/bin/python执行migration_series.py，
传--private-plan、可选--data-roots及独立--output。正式部署前仍需接入协调器，
冻结外部种子清单，验证完整三轮合并训练和恢复，满足原规范1280局配额及所有门槛。
