# 塔血拟合：本机持续采新数据，每批八轮

## 用户变更与历史边界

用户要求停止反复训练同一组状态，改为本机产生新数据、每组训练8轮。旧 `sspi-tower-shared-fit.v1` 在收到请求时正在第11轮；通过已实现的整轮边界暂停机制，完整保存第11轮模型/AdamW后退出，未启动第12轮。旧数据、失败结论、所有epoch记录和checkpoint保持原身份。

新的训练协议为 `sspi-tower-shared-fit.v2`，串行调度协议为 `sspi-tower-refresh-series.v1`。每批最多8轮；正常完成恰好8轮，失败或用户暂停时只认完整提交边界。不存在第9轮继续复用同批数据的路径。

## 固定流程

1. macOS单actor复用已运行的ARM64 Android Emulator和现有event-json-delta端点，采32局训练、8局验证。双方仍使用原 `seed=20261103` 随机初始策略直出，50%独立监督点，0 MCTS；用户只改变数据更新周期，没有改变采集策略协议。
2. 每批都有新run ID、game/seed清单，实际match及rollout seed与旧48局和此前所有新批次显式排除重叠。自然平局排除、不补采；截断/失败保留原始记录并暂停检查。旧最终留出数据不进入训练。
3. 导出完整原始FAIR输入，核对episode、动作账本、目标及分片SHA，再完成压缩传输。完整传输之后才启动服务器trainer；不使用旧512维表示缓存。
4. 首批明确继承旧run暂停边界的完整模型和AdamW；后续批次继承上一批按验证MSE选出的最佳完整模型和对应AdamW。原随机策略的采集身份与正在训练的模型身份分开记录，不每批重新随机初始化。
5. 每批训练8轮，所有有效训练状态每轮各一次；lr1e-4、weight_decay1e-4、batch256/microbatch16、clip1和纯塔血软标签CE保持。共享主体/价值头更新，策略头冻结。验证从不参与梯度。
6. 保存latest及best；无论最佳出现在第0至8轮的哪一轮，完整报告尝试步数和选中步数。模型/AdamW共同恢复后，记录该批数据已消费；即使选择epoch0，也不允许把该批输入再次当新数据训练。
7. 完成8轮后如未达到已有拟合诊断条件，自动采下一批全新32＋8局。达到条件则结束本轮启发式拟合诊断，不自动接SSPI训练或胜率评估。

旧run第11轮checkpoint文件SHA为 `efaa23a34d5065a0e8b0407c2016847e3b40d7739685012ae3dc1ca95716e18e`。首批续接需要显式指定该文件及SHA；禁止凭文件名推断权重、自动混用旧实验或旧三分类模型。父checkpoint协议、架构、随机初始化身份、策略头SHA、优化器设置及数据消费历史均校验。

每批验证局不同，跨批原始MSE不直接视为学习曲线；须同时看当批起点和当批常数基线。反复用于选择的验证集仍只提供拟合诊断，不冒充独立留出验收或实战强度。

## 操作与恢复

冻结代码同时部署到Mac和服务器，启动前核对训练源码SHA。`tools/run_tower_refresh.py` 在本机调度，机器路径、远程地址和probe路径均通过参数传入，不写入公开默认值。

```sh
PYTHONPATH=FROZEN_SOURCE/firstlight_CR python FROZEN_SOURCE/tools/run_tower_refresh.py \
  --series-dir SERIES_DIR --series-id NEW_SERIES_ID --frozen-source FROZEN_SOURCE \
  --initial-model ORIGINAL_RANDOM_MODEL --excluded-seeds OLD_SEED_LIST \
  --probe EXISTING_MATCHING_PROBE --remote TRAINING_HOST \
  --remote-root REMOTE_RUN_ROOT --remote-python REMOTE_PYTHON \
  --parent-checkpoint REMOTE_PARENT_CHECKPOINT --parent-sha PARENT_SHA
```

本机系列内端口锁和调度器锁、远端trainer锁防止本流程重复actor；启动新系列前另行检查现有actor和原生槽位。恢复时检查PID对应的完整命令身份、状态及完成回执；已启动的远端trainer不重新上传/覆盖数据，不额外启动进程。孤立启动记录、部分episode、退出且无完成回执等情况暂停检查，不盲目重启。完成回执须为本批8轮、匹配源码/数据身份且策略头、静态缓冲区和checkpoint恢复全部通过。

系列目录的 `STOP_AFTER_BATCH` 在完整批次边界暂停；trainer已有 `STOP_AFTER_EPOCH` 支持完整轮次边界暂停。保留各批原始数据及checkpoint；本机/服务器新批次启动前检查至少8GiB磁盘余量，达到真实资源限制时保全证据并暂停，不自动清理历史。没有固定总批次数或训练总时间上限。

本轮启动检查发现本机进程缺少 `CR_PROBE` 绑定，在对局创建前由attestation拒绝。保留原回溯后，从当前AVD已映射的原probe只读复制到私有运行目录，并用进程级环境变量绑定。没有重编译、替换原生库，未重启AVD、修改adb转发或访问VMware；该二进制不发布、不上传训练服务器。

## 验证范围

定向检查包含原批量/逐条前向与梯度、真实8轮更新及末轮恢复不重复曝光、模型/AdamW父状态续接、旧数据消费拒绝、跨批seed隔离、串行换批及最佳checkpoint身份传递。相关主工程/公开检查与真实新对局结果单独记录。8轮工程fixture通过不等于首个真实新数据批次已训练完成。

## 首批运行快照

截至2026-11-03 15:55 UTC，本机已完成首批32局训练、8局验证采集，40局均为自然胜负；共产生9,001个训练状态和1,871个验证状态，0个搜索根。与此前48个种子对比，40个新种子对重叠为0。训练使用显式指定的父模型和AdamW checkpoint；第1至4轮已完成，第5轮正在运行，每批预算为8轮。验证MSE在第2轮达到当前最佳0.013269，之后两轮有所回升；该结果只是同批拟合进度，不是独立验收或实战胜率。机器可读身份、耗时和当前best checkpoint见[进度快照](SSPI_TOWER_FRESH_BATCHES_RESULTS_2026-11-03.json)。
