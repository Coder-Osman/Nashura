# 最新主工程迁移原生验证

验证已整合的主工程实现，不改变旧冻结部署，不将旧CUDA或恢复结果继承为新实现通过。

从校验过launch、payload及deployment身份的确定性父载荷构建独立部署，明确替换
active adapter、learning、model、policy、search及迁移入口。保留父缓存、确定性运行时、
原始三局整局划分。旧scalar adapter和learning以独立模块名提供数值参考，参考迁移入口
仅重写这两个导入，不调用新KL作为自己的参考。所有变换及新源码均绑定SHA。

六组CUDA对照使用既定FP32损失、梯度及AdamW容差；真实源A逐Tensor继承统计
单独保存。随后从独立随机学生和空优化器开始，保留2轮冻结及解冻日程，不导入
旧实现checkpoint。连续分支在冻结/解冻阶段各保存中点；两个新进程分别恢复，
验证完整网络、AdamW、RNG、游标/下一批、教师及选优历史精确一致。

训练集352行，batch配置256、microbatch1，尾批96不丢弃；连续分支共7次更新，
其中4次256行、3次96行。冻结中点为第1步，冻结参考第3步；解冻中点第5步，
解冻参考第7步。两个恢复分支各前进2步，均包含一次真实256行更新和一次96行尾批。
不把尾批称为256行，不因工程验证调整真实训练数据或日程。

每个阶段有界，失败停止；资源下限32GiB可用内存和5GiB磁盘不替代正式中心200GiB门。
默认使用.35空闲物理GPU1（进程内cuda:0），不干扰GPU0上的旧冻结链路。
本验证不开展正式1280局、G2/G3或模型发布；training_admissible与formal_g2_pass保持false。

公开工具测试：`python -m pytest -q _spec_work/versions/fair-final-active-migration-v1/test_active_payload.py _spec_work/versions/fair-final-active-migration-v1/test_active_resume.py`。
原生启动入口为start_native.py，输入校验过的私有父attempt目录；默认有界6小时。
collect_native.py仅在进程终止后归档，归档成功不等于独立内容验收通过。

独立验收入口为verify_active.py；调用方须提供launch和下载回执的外部SHA，
以及唯一一轮完整整局数据目录。验收器重新核对上传包、冻结部署、四阶段退出和
不同进程身份、报告与六个实际checkpoint的模型/AdamW/RNG/采样/选优内容。
实现身份绑定六个active文件及消费管线；数据和划分从整局重新生成，不只比较
报告中的成功字段。冻结encoder须保持、解冻须改变，恢复结果须与连续分支精确一致。
默认严格要求352行、batch256、microbatch1和96行尾批；小fixture只能显式关闭
原生日程检查，输出native_schedule_verified=false，不作为原生恢复证明。

```sh
python verify_active.py ATTEMPT --launch-sha LAUNCH_SHA --receipt-sha RECEIPT_SHA \
  --data-roots COMPLETE_DATA_ROOT --output VERIFICATION_JSON
```

验收会拒收失败记录、缺失报告/checkpoint回执、重封装后的错误游标/配置和非有限
指标。它检查归档内容与执行身份，不重跑CUDA张量比较或原生对局来源探针；输出
明确保留native_game_provenance_reverified=false及各正式门false。三局数据原生
来源的独立验收仍以原始数据证据为准。
