# 嵌套历史评测种子补验

2026-10-22 完成 clean-screen、clean-confirmation、round1-screen 与
round2-screen 四组历史评测的只读回收和保守种子排除。这里只关闭本批来源核验，
不关闭 G0/G1、不启动正式 G2，也不重新证明历史模型强度。

## 实现与核验

`collect_nested_seed_history.py` 固定四组远端路径，分别收取 manifest、results、
done 及 evaluate_policy.py，共16份来源；回读前后 SHA 一致，原字节与回执保留。
`audit_nested_seed_history.py` 核对完整换边任务、显式比赛种子、模型与执行代码
身份、尝试编号、终局记录及结果汇总，再以 JSON Pointer 补入来源映射。

四组共3,200条结果、1,600个不同种子，均新增到 evaluation 排除集合。
总排除7,160个：training 1,136、value_training 4,096、evaluation 1,928；
三类交叉重叠为空。训练与价值训练清单保持原字节，重入及独立重算产物完全一致。

5条旧记录的 reason 为 draw_or_unexposed_tiebreak，只表示平局或未公开的
加时结果。实现拒绝把这些记录改标真实平局；本批所有结果均不进入 WDL 训练。
存储的执行代码身份匹配，不等于重新验证历史运行进程或逐局原生终局。

## 测试与证据

- 新专项54项通过，覆盖真实来源、不可变重入、任务/模型/代码/终局语义破坏、
  回执和源文件篡改、基础清单身份拒收及不明确结果边界。
- 种子登记与历史核验联合190项通过，4.33秒，零失败、零跳过。包含上述专项，
  两组数量不相加。见 tests-seed-history-with-nested.xml。
- nested-seed-history-evidence-20261022.json 固定来源、实现、测试及产物 SHA 和
  大小；本机保留完整原始附件，公开快照提供源码、XML及摘要，未打包私有来源。

历史全项目种子普查仍未完成，更早实验及未执行的嵌套计划仍待核验。
回读时间不证明历史事前登记；未创建正式 G2 任务登记，不新增门 PASS。
