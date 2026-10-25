# 整轮补充采集的协调预检 v1

依据冻结TRAINING_SPEC §4.3–4.4、ACCEPTANCE_GATES G1/G2A，补齐已有逐任务
采集入口之前的整轮准备步骤。独立工程版本，不改运行中Full128或terminal恢复。

## 输入与执行顺序

输入是同一轮所有FairFinalSelectedCollectionRun/v1请求及各自外部文件SHA，
另固定部署目录及deployment.json原始SHA。复用现有部署验收器、请求校验器和
v2种子登记验收，不另造种子、选优结果、数据回执或采集成功标记。

先检查完整历史轮次与外部种子来源登记，要求请求恰好覆盖当前manifest全部
任务，不能缺局、重复任务或重复请求。来源模型、选优checkpoint、计划、登记、
配置、父数据路径和引擎输入须整轮一致；只允许任务、独立输出根和端口不同。
仅准备首次attempt，重试调度留待后续接口。拒收重复/缩写命令选项，输出根
必须未使用且互不嵌套，协调输出与部署、数据、输入和采集根隔离。

所有静态检查通过后，逐任务在独立子进程调用部署内原collect_selected.py
的--preflight-only。原入口实际核验父历史、terminal选优checkpoint及模型；
本工具核对其模型/计划/登记身份与engineering范围，整轮network SHA必须一致。
每个子进程最多3600秒，可显式设置1–14400秒；超时结束该子进程，停止整轮。
此阶段不启动worker或引擎，不创建采集数据目录。

预检前后核对请求、全部配置与来源、权重、部署文件、引擎依赖和完整父数据树
的文件集合、SHA及字节数。父数据中新增文件、符号链接、输入变更或预检意外
创建采集根均拒收；原输入不复制、不删除、不改写。目录仅含文件的身份核验
不是对外部进程的锁；后续执行仍必须走worker自己的完整预检。

所有任务预检成功后，最后原子发布prepared.json，内含逐任务预检日志身份、
请求身份和现有worker的命令参数数组。prepared_only仅表示本次准备完成；
不自动执行命令，不证明后续命令启动时输入仍保持相同。
失败保留日志和failure.json，不发布prepared.json，不重用失败目录。

## 使用

```sh
python preflight_round.py --engineering-acceptance \
  --requests REQUEST_1.json REQUEST_2.json \
  --request-shas REQUEST_1_SHA REQUEST_2_SHA \
  --deployment-root FROZEN_DEPLOYMENT --deployment-sha DEPLOYMENT_SHA \
  --output NEW_PRIVATE_PREFLIGHT_DIRECTORY
```

请求、日志及prepared.json含私有路径、数据和来源身份，只保存私有实验目录。
公开仓库仅保存本源码、测试和证据摘要。不得把准备结果当作原生采集回执。

## 验收边界

专项使用明确的假checkpoint、父数据及预检子程序，实际执行独立子进程，复用
真实历史/种子/请求/部署静态验收器。它证明协调顺序、覆盖检查及失败隔离；
不证明真实模型加载、原生终局、round2、连续补充梯度或跨进程训练恢复。
原有真实terminal结束前，本版本不会借fixture进入原生采集。

本版只补齐整轮准备入口，尚未接入正式1280局调度、执行/回收、失败重试及
下一轮训练状态机。后续仍须独立验证每局原生来源、整轮合并、选优交接及恢复。
training_admissible、formal_g2_pass、gates_closed恒为false；G0/G1与34/43
登记不因工程测试增加而关闭，不启动G2–G8或五机放量。
