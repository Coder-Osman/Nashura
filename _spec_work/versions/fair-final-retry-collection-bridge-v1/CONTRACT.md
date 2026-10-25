# 超时恢复terminal结果到补充采集包的独立交接

本版本补齐selection-timeout-v1的独立retry归档与现有补充采集入口之间的接口。
不改旧选优格式，不向其伪造execution或completion；正在运行的恢复部署保持原样。
仅在停止结果独立内容验收通过后准备新目录，不启动采集或正式迁移。

## 准备与复核

```sh
python prepare_collection.py RETRY_ATTEMPT --launch-sha LAUNCH_SHA \
  --receipt-sha RECEIPT_SHA --data-roots COMPLETE_PARENT_DATA \
  --output NEW_PRIVATE_HANDOFF
python verify_collection.py NEW_PRIVATE_HANDOFF --expected-sha HANDOFF_SHA \
  --parent RETRY_ATTEMPT --launch-sha LAUNCH_SHA --receipt-sha RECEIPT_SHA \
  --data-roots COMPLETE_PARENT_DATA
```

先固定launch/receipt，要求worker、训练子进程和GPU租约已释放，无失败结果；
实际调用verify_retry.verify，重验父超时与原始交接、冻结部署、完整数据身份、
terminal内容和超时历史前缀。要求真实352行训练日程、更新步数增加，所有准入门
保持false。现有验收器不重新证明对局原生来源，该范围在交接中明确保留。

由归档inputs重验原第7步交接和载荷；复用build_from_parent，仅更新现有采集
入口及其已登记依赖，冻结训练源码、配置和确定性环境原字节保持。交接绑定
retry launch、receipt、选优checkpoint、报告、超时checkpoint及内容验收摘要。
不复制父失败为新成功，不把timeout checkpoint直接作为terminal模型。

父归档前后重算完整树摘要、SHA及字节数，拒绝符号链接、额外未登记文件和变更。
输出必须全新且与父独立；五个数据文件逐个flush/fsync，再与预期字节数和SHA
逐项核对，最后write-once原子发布
handoff.json。写出失败保留failure.json及部分文件，不重用或删除现场。
输出可包含权重和私有计划，只写私有目录，禁止纳入公开仓库。

verify_collection重新执行父独立内容验收并确定性重建全部五个文件，逐字节比较；
同时固定外部handoff SHA、构建器身份、严格文件集合和prepared-only范围。
即使输出文件和清单同时重封装，仍须与真实父重建结果一致。重验成功不产生
原生采集凭据；后续沿用collect_selected的完整父历史预检及外部seed事前登记。

## 范围

所有结果固定training_admissible=false、formal_g2_pass=false、gates_closed=false，
native_collection_executed=false。mock成功测试仅证明接口和拒收机制；真实
terminal交接、round2原生、连续补充梯度/恢复及整体G0/G1仍单独验收。
