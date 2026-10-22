# 最新实现独立原生验证：发现数值回归，修复后重新验证

代码基线为已整合审计分支的Nashura主线 `af498d14371a2ddb91b5b5794fa1a32b4064629a`。
2026-10-22重新fetch并核对全部远端分支，该提交仍为最新main。

独立载荷277文件，active adapter、learning、model、policy、search、fair_migration
六文件与当前源工程逐字节相同，trainer与原冻结父包一致。父载荷、deployment、
逐文件源码及新上传包分别绑定SHA；恢复身份额外包含六个active文件，拒绝跨实现恢复。
scalar参考保留旧adapter/learning，用独立模块导入，不将新KL作为自己的参考。

新增10项CPU工程测试通过（最近重跑32.96秒，见tests.xml）：源码覆盖与参考隔离、父包/
部署篡改、路径逃逸及重复成员、变换锚点失配、冻结/解冻阶段真实模型与优化器
恢复，并拒收变更后的参考SHA和训练指标。测试夹具仅证明工具行为，不替代CUDA。
首次恢复测试沿用patience=1导致小模型在第6步提前终止；测试夹具改为patience=2
后重跑通过，原生训练配置没有因该夹具调整。

首次尝试native-active-migration-1792675967128763000在183.464秒失败并自动停止；
完整失败归档287文件且部署身份一致，后续reference和两个恢复阶段没有运行。
冻结三对通过，解冻第一对AdamW更新超出既定容差：
encoder.relation_transformer.blocks.1.spatial_bias.2.bias第3项差4.107132554e-6，
两路梯度为-4.321106667e-11与-4.753217264e-10。损失和梯度比较通过，
但近零梯度的差异经AdamW放大；保留失败，不放宽atol=3e-6、rtol=2e-5。

修复learning.py：编码器解冻时，教师和学生delay均保留逐位置路径；
编码器冻结时保留合批。回归测试明确检查两侧batch调用，11项learning和
26项migration通过；这些CPU测试不替代CUDA。源码修复基于af498d1。

修复后独立尝试native-active-migration-1792676550169895000，.35物理GPU1，
PID50734/start26665117，最长6小时。快照1792676859：benchmark exit0，六对
损失、梯度、AdamW比较均通过，解冻三对最大梯度差为0，已进入reference。
单帧配对加速比中位数1.01174；冻结两个可ACT帧分别约6.14及6.54倍，解冻
保持scalar约1倍，WAIT帧约1倍。不将不同阶段时长中位数之比视作总体加速。
详见cuda-comparison-evidence-20261022.json；原始附件保持私有。
后续自动执行frozen恢复、trainable恢复，任一失败即停止，最终归档独立验收待完成。
没有启动正式数据采集或模型发布，旧失败部署和其它运行中任务保持原样。

原Full128搜索任务保持冻结部署：快照1792676578已提交92组/184根/23552模拟，
tick3510玩家0搜索中；尚未完整终局。
原batch-v1链路仍在selection，前四阶段正常结束；这些旧实现结果单独保留。

G0/G1仍开放，限定子项34/43保持；G2–G8未运行。新CUDA与跨进程恢复仍待完成
并独立核验归档，training_admissible=false、formal_g2_pass=false。
