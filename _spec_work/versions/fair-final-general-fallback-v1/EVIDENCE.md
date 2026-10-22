# 一般历史退化路径验证结果

2026-10-22：最终73项测试通过，6.196秒，零失败、错误及跳过。
tests-initial.xml、tests-integration.xml与最终测试存在重叠，数量不累加。
源码、契约、测试及报告的SHA保存在verification.json；依赖history-v2冻结
manifest中的564个文件逐项复核一致，零改动。主项目与运行中的部署未修改。

新增provider仅在局部conditioner明确报告不支持、且旧锚点清理成功后，
转入独立初始化与有界历史重演；协议、同步、恢复、清理及超时错误直接失败。
测试覆盖预算、共享deadline、RNG分离、输入非干扰、历史前缀与lease生命周期。

双方pending内存集成采用真实重条件化、历史重演与搜索实现：每玩家重复两次，
四根共512有效模拟；每根128，未完成边回传为零，恢复后pending及根状态一致，
相同RNG的访问、动作及manifest一致。pending和native manifest由fixture构造，
该结果不证明原生合法pending可达，也不替代原生一般根构造或完整搜索局。

本版本尚未部署。G0/G1继续开放；training_admissible=false、formal_g2_pass=false。
后续仍须完成原生一般pending、完整Full128终局及独立恢复、总体先验校准。
