# 第 10 / 30 / 60 / 70 轮原生对局评估

2026-09-18 01:23–02:24（北京时间）在 172.16.48.37 完成，共 2,592 局。全部逐局记录已下载并复算通过，四个新增评估实例已自动关闭；02:24 复查时原 12 小时训练仍在第 90 轮运行。

本地最终报告：[胜率矩阵与置信区间](/Users/gujiga/Documents/ChatGPT/CR/firstlight_CR/evaluations/checkpoint-roundrobin-10-30-60-70-20260918/report.md)。同目录保存 `report.json`、`validation.json` 与逐局证据包 `evaluation-evidence.tar.gz`。

本次结果支持 10→30→60 的实力提升；70 对 60 为 229 胜、203 负，胜率 53.01%，95% 配对区间 49.07%–56.94%，尚无明确证据表明 60→70 有显著提升。全部对局无平局、无非法动作；两次原生事件缓冲区错误均按原种子重跑并正常终局。

- 六种两两组合，每组 216 对同种子换边对局，即 432 局，共 2,592 局。
- 石头人镜像、11 级、采样温度 1、无终局奖励塑形。只计正常终局，截断和超时触发重试。
- 得分率把平局计为半分，95% 区间按种子对 bootstrap；胜率为胜局除以全部对局。
- 四份权重 SHA256 及模型契约已经核验。第 10～70 轮七份本机备份位于 `checkpoints/rl-milestones-001-070/`，均通过加载、轮次和 AdamW 状态验证，详见其中 `manifest.json` 与 `validation.json`。

远端任务目录：

`/data/firstlight/checkpoint-roundrobin-10-30-60-70-20260918`

关键文件：`config.json`、`engines-ready.json`、`probe-install.json`、`status.json`、`tournament.log`、`worker-*-attempt-*.log`、各对阵目录中的逐局 JSON。完成时生成 `report.json` 和 `report.md`。

独立评估实例为 `emulator-5580/5582/5584/5586`，各 6 引擎，主机端口分别为 `31089–31094`、`31189–31194`、`31289–31294`、`31389–31394`。任务结束会自动关闭这四个实例。不要停止训练实例 `emulator-5554/5556/5558/5560` 或使用它们的引擎端口。

训练快照代码：`/home/zhichun/firstlight_CR/runs/event-mirror-v2-12h-20260917T130304Z/code`。

任务执行脚本在本仓库 `tools/event_checkpoint_roundrobin.py`；远端副本为 `tournament.py`。统计模块复用 `tools/event_rl_fleet_evaluate.py`，其 8 项测试已通过。任务分配校验覆盖 2,592 个唯一对局及每个种子的双侧对局。

未启用应用定时任务。原有 12 小时训练继续运行。
