# 修复后完整局与训练可用性试验

## 运行身份

- SSH：`zhichun@172.16.48.37`，仅使用 CLI。
- 远端工作目录：`/tmp/fair-refill-20261009/project`。
- 运行输出：`/tmp/fair-training-probe-20261010-1759`。
- Episode：`fair-training-probe-full-1759-20261010`；seed：1759。
- Worker PID：2136310；采集 PID：2136410。
- 冻结父模型：`e9f7c84a3e5f84a4d625c3f9a68e332a318a2aedc06e196da55bdb00a66bd1f0`。
- `cuda:2`，FP32，双方每根精确 32 次模拟。
- 复用已验证的单 active belief world 配置，不扩并发、不增加世界数量。
- 实际局端口 30069；双方信念世界端口 30070、30071。
- 搜索配置、训练配置、引擎及 Python 源码身份保存在独立 `manifest.json`。
- 旧局日志和旧运行 manifest 保留，未替换父 checkpoint。

## 流程与验收边界

`run_training_probe.py` 启动独立 worker，执行完整局采集。采集成功退出后，自动加载
`episode/completed.json` 与 `episode.terminal.pt.gz`，核验校验和、契约、双玩家样本、
32 次访问、模型/配置/运行身份、终局标签以及发布数据与终局恢复包的一致性。

数据通过后，用独立模型和全新 AdamW 优化器调用现有 `SearchLearner`、`ReplayPool`：
按每个新增决策两倍样本预算、有效批大小 256 执行试训，检查有限损失/梯度、策略头与价值头
实际变化、独立 checkpoint 读回、回放状态和训练游标恢复，以及父 checkpoint 未变。
样本预算不足一批时保留数据验收结果，试训门槛不通过，不缩小正式批大小或补造样本。

这是单局诊断试验，明确不通过正式 128 局代际门槛、不发布冠军、不替换自博弈权重。
正式训练仍须满足完整代际采集和候选评测门槛。

## 输出

- `state.json`：`collecting`、`validating`、`data_validated`、`training_probe`、`completed` 或 `failed`。
- `collection.log`：搜索根进度、每根耗时和最终采集收据。
- `worker.log`：验收/试训状态和异常。
- `data-validation.json`：实际完整局通过数据门槛后生成。
- `training-validation.json`：真实样本实际试训及 checkpoint 检查全部通过后生成。
- `diagnostic-only-checkpoint.pt`：独立诊断权重，不用于正式模型发布。

## 已完成验证

实际 CUDA 分配/计算和 1755 模型加载均成功。NVML 有驱动/库版本告警；该告警不等同于
CUDA 推理失败，未为此重启共享 GPU 机。启动前约 71 GiB 可用内存，短时 vmstat 采样
未见持续换页，仅启动一个 actor。

验收脚本使用明确标记的合成测试数据验证训练/读回及故障拒收，首次结果：
`4 passed, 1 warning in 125.23s`。修正独立脚本的模块导入路径后复测结果保存在远端
`harness-tests.log`，结果为 `4 passed, 1 warning in 123.98s`，已同步本地。
该单元测试结果不作为实际新局训练成功证据。

实际新局首根：玩家 0、tick 390、32 次访问、48.49 秒；信念接受一个世界，未记录错误。
玩家 1 同 tick 首根：32 次访问、59.15 秒；信念接受一个世界，未记录错误。

实际局已完成：tick 4909，owner 0 胜，皇冠 1:0，162 条有效决策，双方各 81 条，
所有根均为 32 次访问。采集耗时 9737.76 秒。
`data-validation.json` 与 `training-validation.json` 已同步本地；真实样本完成一个
有效批 256 的 AdamW 步，策略与价值参数均改变，checkpoint 与游标恢复验证通过。
这是诊断试训，不是正式代际训练，父权重未变，候选未晋级。

## 原局视频证据

`export_render.py` 从原局训练记录导出 73 条真实公开执行事件；不是把 selected 计划
直接当作已执行动作。原生终局的 combat card_play 环保留 61 条重叠出牌，逐条匹配
公开事件的 owner、cardId、执行状态 tick，且覆盖最后决策到终局，未发现额外尾段出牌。
完整终局原始捕获存为 `render-export/original-final-atomic.json.gz`，主比赛未被重置。

原采集没有独立保存队列提交收据和完整初始循环。用闲置引擎按同构建、同 seed 重建
开场，检查训练记录中的双方初始五张牌，再按原公开事件的时间、位置和卡牌重放。
重放终局的 tick、胜方、皇冠、所有 players 与 objects 字段均与原终局完全相等。
`verification-receipts.json` 明确属于本次验证重放，不冒充原采集提交收据。

`render_episode.py` 复用本机 Renderer 的无窗口启动、配置和资源关闭逻辑，使用独立
FAIR 数据校验，不伪装成旧 PPO 调度契约。仅调整发牌槽位以对齐手牌和循环，不调整
动作时间或坐标。保留已有客户机录像，渲染结束关闭本任务启动的模拟器。

本次录像 73 张牌全部 succeeded；终局比分及双方剩余塔血、手牌、循环均与原局一致。
原局采集在首次终局 tick 4909 停止；录像以约 0.5 秒轮询，最后 live 采样为 4895，
首次 ended 采样为 4916。渲染端继续更新到轮询观察点，因此观察 tick 差为 +7
（0.35 秒游戏时间）；仅确认原终点处于测得的转变区间，不宣称精确结束 tick 相同。
校验器核对该区间、采样耗时对应的 tick 上界以及最终塔血，超出区间则拒收。

最终视频：`render-export/fair-search-seed1759.mp4`，1080×1984，30 fps，
255.833 秒，61,338,560 字节；恢复正常游戏速度，并在画面外标注双方、seed 和权重身份。
本机 FFmpeg 缺少 drawtext，改用 Pillow 生成图片标注条后 overlay，不改动游戏内容。
ffprobe 与全帧解码通过，开场 4 秒、中段 120 秒、终局 251 秒抽帧目视检查通过。
73 次出牌全部成功（owner0 38 次、owner1 35 次）；7 项回放契约测试通过。
验收结果存于 `render-export/video-validation.json`，模拟器已关闭，adb 设备列表为空。
视频 SHA256：`5a228988d21c229073a1757a641033b8bdc7128febcd8f33a4a37eb7a834cb33`。
