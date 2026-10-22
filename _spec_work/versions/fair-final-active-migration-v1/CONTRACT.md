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
