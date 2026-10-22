# 采集优化实施与验收（2026-09-18）

实施范围：常驻采集的共享推理、按需观测、WAIT 验证快路、传输 A/B、原生采集索引及分段计时。完整局、30 tick 上限、公开信息契约、无跨决策神经记忆、行为 checkpoint 身份和 GAE 均保留。

## 已实现

- `native_runner/training/event_inference_service.py`：同机私有 Unix socket 推理服务，默认最大 16 行、额外聚合等待上限 2 ms。尾部单请求及时执行。仅 supervisor 在轮次 barrier 加载权重；请求和回复绑定完整 checkpoint、推理 artifact、模型契约及请求序号。加载失败清除服务身份，旧版本请求报错。
- 推理 worker 只保留模型元数据，无需每进程装载模型。CPU 观测和 mask 经 protocol-5 NumPy buffer 编码，避开 PyTorch 逐 storage archive 的 IPC 开销。socket 在权限为 0700 的临时目录内，仅用于同用户本机通信。
- `EventCardPolicyV1.select_action(sample_uniforms=...)`：可选逐请求四个独立均匀随机数，依序对 gate/card/cell/delay 逆 CDF 采样。每局 CPU Generator 独立，排队顺序不消费别局随机数。原采样路径默认保留；新协议名 `per-match-cpu-uniform4.v2`。这是同一行为分布的新采样算法，不承诺与旧 multinomial 的同 seed 动作相同。不同浮点设备或批大小也不承诺位级概率一致。
- `BattleEnvV1.step(defer_observations=True)`：完整执行 native 推进、事件消费、belief/pending 更新、奖励和终局计算，但返回空观测字典。collector 在新决策或到期计划执行前，通过 `observe_dual` 取得当前真实局面。通过 `current_tick` 读取时钟，避免使用上一帧观测。
- 普通 WAIT 在 owner/compound 校验后直接通过；WAIT_UNTIL_AFFORDABLE 和出牌保留完整验证。
- `timing_profile` 记录准备、native/typed binding、事件核对、观测构造耗时。`channel_loop_profile` 扣除 priming，计时范围与主循环一致；累计 channel 数据仍保留。
- `capture_mode` 支持配置 `json`/`zlib-json`。没有切换到缺少完整契约信息的 compact。
- `capture_scene_index.inc`：在单次 atomic capture 作用域内，复用 pointer→slot、native ID 唯一性索引。空指针、重复指针、重复 ID、不可读对象仍按旧逻辑处理，退出 capture 后销毁，不跨 tick 缓存地址。
- `capture_profile.inc` 和 `capture-profile` 控制命令：累计 native step、capture、ordinary/rich 构造、压缩、发送纳秒数。ordinary/rich 嵌套于 capture，构造时间包含 JSON 编码，不应解释为纯扫描时间。查询必须在 batch session 关闭之后。

## 配置入口

`PersistentFleet` 读取 `collector_manifest`、全局 `collector_options` 以及每台机器的 `collector_options` 覆盖。示例：

```json
{
  "collector_code": "collector-optimized-code",
  "collector_manifest": "collector-optimized-manifest.json",
  "collector_options": {
    "inference_device": "cuda:1",
    "inference_max_batch": 16,
    "inference_wait_ms": 2.0,
    "defer_observations": true,
    "capture_mode": "json"
  }
}
```

共享服务每轮在开始派发前验证并加载 artifact，上一轮所有 worker 完成后才换权重；worker 重试仍从同 seed 重新开始完整对局。推理服务失败时不会静默回退成另一采样路径。

`inference_device` 省略即可使用旧 CPU 推理。`sampling_protocol: "per-match-cpu-uniform4.v2"` 可让 CPU 对照组使用同一随机数协议。所有开关均须在 host 启动前确定，运行中不修改。

## 验证证据

本地记录：`evaluations/collector-optimization-20260918/`。

生产原始代码、旧学习器和正式训练权重未被覆盖。远端以生产 `collector-code` 为底稿新建独立快照，逐文件原子替换避免改写硬链接源；法术约束和生产事件增量 cursor 均保留原状态。本地 rollout 中已有的法术约束没有混入本次正式契约对照。

第一阶段使用最终 201 权重、4 个相同 seed，在原引擎上测试五种采集路径，共 20 局。全部通过未修改的旧 learner 完整性/概率重放 preflight，最大绝对 log-ratio 小于 6.7e-6，clip fraction 为 0。

| 路径 | 四局主循环总时间 | 四局返回字节数 | 语义核对 |
| --- | ---: | ---: | --- |
| CPU eager | 130.67 s | 172,364,897 | 对照 |
| CPU 按需观测 | 117.66 s | 172,364,897 | 动作、奖励、输入逐项精确相同 |
| CPU 按需观测 + zlib | 119.91 s | 11,060,222 | 动作、奖励、输入逐项精确相同 |

按需观测在这四局减少约 10% 主循环时间；压缩减少 93.6% 字节，但没有改善这组单机耗时，故不据此默认开启压缩。冷启动开销另计，不能把上述总和解释成一轮 wall time。

GPU 路径使用新采样协议，四局共 184 行，CPU 旧协议为 116 行；两者对局不同，不可直接比较 wall time 推断加速。GPU json/zlib 对照的动作和输入相同，概率/熵只出现批形状浮点误差，联合 log-prob 最大差约 2.87e-6。后续满载对照统一 CPU/GPU 的采样协议。

原生测试采用独立候选库并在结束后恢复原库。首次启动验证遇到 adbd/端口转发的空响应，已保存失败记录；不能把这次启动失败作为引擎性能数据。新版候选库随后通过四局实测，动作、奖励、输入精确一致。四局主循环合计 117.27 s，相比原引擎 + 按需观测的 117.66 s，差异约 0.3%，尚不足以认定有稳定加速，故暂不默认替换原生库。

原生累计 profile：step 29.03 s，capture 13.34 s（其中 ordinary 0.835 s，rich 12.153 s），send 0.124 s。rich 包含字段读取、校验和 JSON 构造，进一步优化应针对该路径做剖析。测试中原库文件已经恢复；端口启动校验单独重建转发，并保存所有六个端点的原库 SHA attestation。

独立进程测试还发现并修复 protocol-5 reducer 引用命令行服务 __main__ 名字的问题：现在使用稳定的 torch.from_numpy 引用。相关本地八项测试通过，包含新加的跨进程入口回归；先前完整 222 项测试结果另附。


## 24 路完整局验收与已启用配置

同一台 .34、相同 24 个 seed、同一 201 权重、同一逐局 uniform4 采样协议；CPU/GPU 都启用按需观测。每种路径连续两轮，以第二轮衡量常驻性能。

| 路径 | 冷启动轮 | 常驻轮 | 每轮完整局 / 决策 |
| --- | ---: | ---: | ---: |
| CPU 独立推理 | 104.89 s | 84.36 s | 24 / 967 |
| GPU 共享推理 | 111.61 s | 66.83 s | 24 / 967 |

共享 GPU 常驻轮耗时减少 20.8%，吞吐提高 26.2%。CPU 推理累计 273.23 s，GPU 路径含 IPC/排队累计 94.01 s；这是各对局之和，不是 wall time。GPU 冷启动轮更慢，服务/CUDA 初始化成本需要多轮常驻摊销。

四轮共 96 局、3,868 行，动作、输入张量和奖励精确一致；概率/熵最大差 8.58e-06。全部由未修改旧 learner 重放通过，最大绝对 log-ratio 1.53e-05，clip fraction 为 0。不同 seed/硬件不保证动作位级相同；这里的一致性是本次真实对照结果。

另外在 .35/.37/.39/.41 各完成两局，共 8 局；常驻 worker PID 不变，服务从 201 权重切换为 200 权重，全部完整局、无重试、旧 learner preflight 通过。加上首阶段 20 局及原生候选 4 局，本轮共 128 局成功验证；失败的 IPC 首次尝试不计入成功样本。

五机独立快照均已部署，最终 manifest SHA-256：d6800918efb85942ff2868c3a5ed7cf4b1287c44ae719f0a09fcb5067ab88d45。

中央 fleet.json 已启用本文配置：cuda:1、max batch 16、额外聚合等待 2 ms、按需观测、json。训练预算仍为原定 30 轮，已完成至 201；本次没有启动新训练。原配置备份位于远端 run 下 fleet.before-collector-optimization.json，切回该配置即可使用原采集快照。原 learner、正式权重和原引擎库保留。

原生候选与 zlib 均已实现并验证，但这次没有测出稳定 wall-time 优势，默认不启用。静态协议字段裁剪/compact 契约迁移未加入：当前压缩已能消除大部分传输量，实际瓶颈主要在 native 推进、rich 构造和 Python typed binding；本次不增加额外协议迁移。

本地先完成 222 项回归；IPC 修复后八项相关测试再次通过，其中包含新增的独立进程入口测试（合计 223 个不同测试）。证据目录包含真实轨迹校验、满载计时、跨机热切换、恢复后的端点 attestation 和可复用的最终增量源码包。
