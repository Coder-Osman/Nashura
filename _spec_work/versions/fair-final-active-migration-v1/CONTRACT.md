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

## 选优续跑的独立交接准备

`prepare_selection.py`只在父active归档全部通过上述原生日程独立验收后准备交接。
必须提供外部固定的launch及下载回执SHA，并重新核验完整数据；fixture日程、
失败/未完成父及旧batch链路均不具备交接资格。

```sh
python prepare_selection.py PARENT --launch-sha LAUNCH_SHA --receipt-sha RECEIPT_SHA \
  --data-roots COMPLETE_DATA_ROOT --output INDEPENDENT_PRIVATE_DIRECTORY
```

输出包含原字节冻结载荷、完整第7步trainable-reference checkpoint、重算验收报告
及最后发布的selection-handoff.json。完整checkpoint保留模型、AdamW、RNG、
数据游标和选优历史；不只导出模型权重，不重建优化器。配置、损失和实现身份
沿用父包，max_updates为空，后续终止选优仍按原早停和epoch日程执行。

输出目录必须独立于父归档，已存在目录拒绝覆盖。载荷路径、重复成员、源码与
checkpoint身份均复核，文件SHA及交接整体摘要写入记录。该工具不连接GPU机、
不启动训练、不宣布terminal选优通过，状态明确为prepared_only_not_executed；
原生父恢复验收未完成时仍应等待。私有交接目录含权重与计划，不纳入公开仓库。

旧native-batched-migration-1792664800317034000在selection阶段耗尽四小时总预算，
退出124；前四阶段exit0不能替代全链路通过。失败现场及最后第7步checkpoint
独立保留，不跨实现交给本active流程，也不将其作为terminal父启动round2。

## 独立选优执行与终止内容验收

`run_selection.py`接受外部固定文件SHA的上述交接目录，重新核对父证明、全部
输入字节和安全载荷路径，再复制到全新独立输出目录。训练命令只引用交接中的
冻结部署，保留完整第7步checkpoint、原seed/batch/microbatch及确定性环境，
使用`--resume-sha`恢复；不加`--max-updates`，不重建优化器或选优历史。

```sh
python run_selection.py HANDOFF --handoff-file-sha HANDOFF_FILE_SHA \
  --source SOURCE_A --data-root COMPLETE_DATA_ROOT --output NEW_PRIVATE_OUTPUT \
  --device cuda:0 --timeout-seconds 28800
```

本工具提供独立1800–28800秒预算和独立进程组。超时先TERM、后KILL；即使
进程组首进程已经退出，也清理其遗留后代。中断或非零退出写failure.json并隔离，
不发布completion。退出0仍要求匹配的terminal报告、checkpoint SHA和冻结部署
字节，成功状态仅为`completed_pending_independent_content_verification`。

`verify_selection_execution.py`独立固定execution及completion文件SHA，重验
部署/命令/环境、Linux PID启动身份、完整整局数据身份和第7步父状态，核对实际
终止checkpoint与报告、父历史前缀、latest/best采样游标和RNG、教师及损失身份。
若best仍来自父历史，须逐语义保留其完整模型/AdamW/RNG状态。第12轮结束不再
洗牌，采样器验证只重放12次洗牌；含Tensor状态通过语义摘要比较。

```sh
python verify_selection_execution.py PRIVATE_ARCHIVE --execution-sha EXECUTION_SHA \
  --completion-sha COMPLETION_SHA --data-roots COMPLETE_DATA_ROOT --output VERIFICATION_JSON
```

验收器不重跑CUDA更新或重新计算验证集模型指标，不重新证明原生对局来源。
CPU小模型仅显式通过helper关闭原生日程验证，CLI始终要求原生日程，Mac执行
fixture的空start_ticks不具备原生执行身份。真实SSH派发/回收及启动前资源/GPU
独占检查仍须单独完成；原生父未通过独立验收前，不启动本续跑。代码与测试通过
不代表真实交接、terminal选优、G2或任何模型强度通过，三个正式准入标志保持false。

```sh
python -m pytest -q test_selection_execution.py test_selection_handoff.py test_active_verifier.py
```


## SSH 派发与回收工程原型

`selection_ssh.py`仅接受已独立验收的私有handoff，使用拒绝未知主机密钥策略；
先检查目标机内存、磁盘、指定GPU UUID/占用与CUDA，再把固定输入、worker和运行时
以原子SFTP上传至唯一attempt目录。远端启动器再次核对配置SHA、完整上传树、源码、
handoff及GPU独占锁。私有attempt日志和权重归档必须保存在公开仓库之外。

```sh
python selection_ssh.py launch HANDOFF --handoff-sha HANDOFF_SHA --host HOST \
  --username USER --private-root PRIVATE_ROOT --remote-base REMOTE_BASE \
  --remote-python REMOTE_PYTHON --remote-source SOURCE_A \
  --remote-data-root COMPLETE_DATA_ROOT --gpu-index 1 --timeout-seconds 28800
python selection_ssh.py collect PRIVATE_ATTEMPT --launch-sha LAUNCH_SHA
```

回收仅在所属worker/子进程及GPU lease均不活跃时读取结果；下载前逐项重核输入、
部署、配置和进程元数据，核对SHA与字节数并原子落盘。后续仍须运行独立
`verify_selection_execution.py`，归档接收不能替代内容验收。全模拟launch与回收
联合测试93项通过；尚未连接真实主机或执行真实父交接。本原型不闭合G0/G1，不构成
terminal选优、正式G2/G3或模型强度证据，所有准入门仍为false。
