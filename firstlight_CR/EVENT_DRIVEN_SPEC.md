# 事件驱动决策架构规范（EVENT_DRIVEN_SPEC）

状态：草案 v1
提出：用户方案（以“任一方出牌”为事件，在事件处推理下一张牌+延迟+位置，或等待下一事件）
目标：把连续时间离散化为事件，推理次数降低 1-2 个数量级；以 HF 数据集预训练替代强制 ACT。

---

## 1. 决策范式切换

- 旧：每 5 tick（决策周期）双方各推理一次，绝大多数决策是 WAIT。
- 新：**仅在“事件”处推理**。事件 = 任一方打出一张牌（公开信息，FAIR 无泄露）；
  兜底事件 = 我方圣水满 或 最大等待超时（防止双方永远等待卡死）。
- 每次事件处输出一个**下一动作计划**：
  - `gate = ACT`：计划下一张牌 `candidate_index` + 落点 `target_cell` + **延迟 `delay_offset`**
    （0.1s 粒度，0~6s，60 档）——即“在几秒后下到哪”。
  - `gate = WAIT`：不创建动作计划，保持等待，直到下一个事件唤醒。
- **计划可被取消（receding-horizon）**：若计划动作执行前触发新事件，旧计划立即失效、
  不执行、不产生动作执行奖励；在新事件处重新推理并覆盖计划。事件本身是新的观测边界，
  不是旧计划的确认。只有原生执行回执确认的动作才计入 ACT/出牌训练标签。
- 若新事件与计划在同一 tick，事件优先：先取消旧计划，再用新状态推理；禁止同一计划
  在旧状态和新状态下重复执行。

## 2. 与现有代码的映射

| 新架构要素 | 现有实现 | 需要的改动 |
|---|---|---|
| 下一张牌 | `candidate_index`（固定步，仅当前合法手牌） | **改为牌身份 `card_id`**：运行时 `_plan_action` 本就按 `card_id` 在执行刻查手牌，且约 2/3 的下一次出牌在触发刻尚未入手/未够费，无法用 `candidate_index` 表达 |
| 落点 | `target_cell` | 无 |
| 延迟 | `delay_offset_ms=(0,50,100,150,200)` | **拓宽到 0.1s×60 档（0~6000ms）**；基准为事件观测刻 |
| 出牌/等待门控 | `gate` | 语义从“此刻 vs 5tick”改为“此刻 vs 等到下一事件” |
| 事件触发 | 无（headless 固定 tick） | **原生“步进直到公开事件”原语**（探针改动） |
| 专家预训练 | cache_builder + train_imitation_cache | 延迟头目标来自回放 20Hz 动作时间戳（已含） |
| 可变时长折扣 | `_rewards` 的 `delta_ticks` γ^时长 | 已有，事件边界需重新校准 |

## 3. 强制 ACT 的取舍

- 旧定时架构：WAIT ≫ ACT（10-100×），门控失衡 → 强制 ACT 保证 ACT 轨迹。
- 新事件架构：每个决策由出牌事件触发，修正标签语义后 ACT:WAIT ≈ **2.44:1**（2000 场
  HF 统计），仍 ACT 占优 → **可取消强制 ACT**，改由 HF 数据集（IL_Replay：252,238 场
  回放、17,836,160 动作）预训练初始化门控/延迟/牌身份/落点各头。
- 保留 `--force-act-probability` 为可关开关（默认 0），兜底给极端被动对局，而非删除。

## 4. 事件定义（初版，公开信息集合）

触发决策的 tick 满足其一：
1. 任一方刚打出一张牌（公开部署事件，`deployment` 记录 `played_card_global_id`）。
2. 我方圣水 ≥ 决策阈值（默认 9.5，防憋费）。
3. 距上次决策超过最大等待（默认 300 tick = 15s）。
4. 对局结束/时间截断。

**观测刻定义（实机探针）**：出牌记录在 tick P，其实体与公开事件环都要到观测 tick
`P+1` 才可见（`tools/event_ring_probe.py`：play 216 → 实体/环出现在 217）。因此事件
决策观测刻 = `P+1`，延迟以该刻为基准；导致该事件的出牌**永不**作为自身答案
（严格 `>` 取下一张）。早期 `>=` 语义让 57.5% 的 ACT 标签退化为 delay=0 的“复制刚
打出的牌”，修正后 delay=0 仅占 1.3%。

## 5. 延迟头（高维预测）

- 离散 60 档：`delay_offset_ms = (0,100,...,6000)`（`EventPolicyConfigV1` 冻结）。
- 训练用**延迟邻域平滑**损失（作者已在 IL 损失实现 `_delay_neighbor_smoothed_nll`，
  `delay_neighbor_weight`），迁移进 PPO 价值/策略侧。
- 目标来源：回放动作时间戳 → 事件观测刻到下次出牌的间隔 → 量化到 0.1s 档；
  超过 6s 地平线记为 WAIT（新事件会取消远计划）。

## 6. 时间语义与 SMDP

- “等待下一事件”是可变时长选项；回报按 `γ^Δticks` 折扣（`_rewards` 已支持）。
- GAE/优势在事件边界用实际 Δticks 做折扣；截断仍以公主塔势结算（沿用 1x 画像）。
- 事件间隔是随机的 → 优势估计需在边界 bootstrap，详见 §7 实现。

## 7. 实现阶段（依赖顺序）

- **P1 延迟头拓宽**（Python/config，先验证最难的高维头）：
  `delay_offset_ms` 5→60 档；模型 `delay_offset_head`/`delay_offset_embedding` 宽度、
  checkpoint 迁移（新输出列/新 embedding 行零初始化，复用 card-runtime 迁移模式）、
  IL 损失已具备邻域平滑。
- **P2 事件循环（Python 侧，先正确后优化）**：
  用 1-tick 步进 + 捕获中的 `deployment` 事件检测来模拟“步进直到事件”，
  在事件 tick 上做决策；新事件到来时取消未执行的 pending plan；先打通语义，再优化吞吐。
  已实现 `native_runner/training/event_driven_runtime.py`，包含最大等待 timeout 唤醒、
  执行回执确认和事件去重。9 个 P2 合约/运行时测试通过。
  - 真机 smoke（`tools/event_p2_act_smoke.py`，fresh match）已验证两件事：
    (1) 在 tick 150 安装合法首牌计划（delay 0）后，`executed_plans` 递增且引擎公开
    出牌环确认该卡（`confirmed_card_play=true`）；(2) 在 tick 211 安装 3000ms 长延迟
    计划并注入对手出牌制造中途事件后，`cancelled_plans` 递增且该卡从未被执行
    （`planned_card_played=false`）。过程中修掉 `_plan_action` 未带 owner 调
    `observe()` 导致 FAIR 观测报错的 bug。
- **P3 HF 预训练**：复用 cache_builder 重建事件驱动专家轨迹（含延迟目标），
  训练事件驱动策略初始化，替代强制 ACT。
  - 观测侧已实现：`event_producer.py`（按专家命令刻批量步进、落到事件观测刻取
    真实张量、配对下一张牌/落点/延迟标签）、`event_cache.py`（`.crev.zst` 分片）、
    `tools/build_event_observations.py`（多引擎并行 CLI）、`tools/event_ring_probe.py`
    （事件环时序探针）。真机 6 场 0 失败、事件捕获 412/422 次出牌，约 41s/场/引擎。
  - 离线标签器 `tools/build_event_dataset.py` 已同步修正语义并重生成 2000 场报告。
  - 模型侧已实现：`event_model.py` 复用 V4 观测编码器/LSTM 主干，新增
    门控（ACT/WAIT）、牌身份（己方 8 行 deck 指针）、61 档延迟、32×18 落点四个头；
    `event_il_loss` 含延迟邻域平滑，超纲/非本副牌的标签只保留门控损失。
    入口 `tools/train_event_pretrain.py` 消费 `.crev.zst` 分片，真机 956 行/386 ACT
    单批前向+回传通过，四头初始损失≈随机（gate 0.685、card 2.13≈ln8、delay 4.08≈ln61、
    cell 6.26≈ln576）。
- **P4 原生 step-until-event（探针 + APK 重打包）**：resident_multimatch 加
  “步进直到公开部署事件/圣水满/超时”原语，一次批量调用内部循环，拿到推理
  数量级的计算收益；随后 48 引擎全量重新部署。
  - 原生已实现：`resident_step_bound_until`（`resident_multimatch.inc`）在每一步
    前后比对 `g_combat_next_deployment_sequence`，命中公开部署/终局/超时即停；
    复用请求头 reserved 作 `flags`（bit0=step-until-event），响应 entry flags 回传
    停止原因（deployment/terminal/max-ticks），协议版本与帧长不变、向后兼容。
  - Python：`resident_batch_channel` 支持 `step_until_event` 与会话级事件模式
    （`batch-*-v1` 握手字符串不变，靠请求 flags 区分），coordinator/proxy 暴露
    `last_stop_flags`；工厂 `*_v4` 加 `step_until_event` 开关。
  - 部署：探针为 JNI `.so`，无需重打 APK；但本机 APK 以 incremental-fs 安装，
    lib 目录只读，需先 `adb install --no-streaming -r -d -g` 重装以得到可写 lib，
    再用 `install_probe_idle_guests.py` 热替换（已备份旧 .so，可回滚）。
  - 真机验证（`tools/event_native_probe.py`，复现回放 84a066ec）：34 次出牌 →
    34 个停止刻**全部等于“出牌刻+1”，0 缺失、0 多余**；36 次引擎往返
    （34 事件 + 1 超时 + 1 终局），5.4s/场（Python 落点法 14.4s，约 2.7×）。
    多 lane 时一次批量往返可让每个 lane 各自停在自己的事件上，是数量级收益的来源。
  - 观测采集已切到原生事件模式：`produce_event_observation_batch_native` 每轮提交
    `WAIT(max_advance_ticks)`，原生在事件刻停下，`previous_action` 由
    `record_expert_previous_action` 直接合成（事件刻牌已离手，无法走候选解析）。
    单 lane 输出与 Python 落点法**逐条一致**（帧 68/190/140、ACT 36/156/110、
    合法 8/57/43）；5 引擎并行 10 场 55s、0 失败、21.6 帧/秒（240 场约 5–6 分钟）。
  - **限制**：原生事件模式必须一引擎一 lane。多条 lane 各自停在不同 tick，
    而探针的原生状态是进程全局的（capture 的 `ended` 读 `g_control_ended`），
    后一条 lane 会继承前一条的终局结果并提前结束（实测 lane 1 在同帧 lane 0 终局刻
    3171/3172 被判终局）。已在 `event_producer` 与工具层硬性拒绝多 lane。
    若要恢复共批，需要让探针的终局/随机数/遥测状态按 slot 完全隔离。
  - 待办：其余 3 台模仿真器（另 18 引擎）热替换新 `.so`；`out/libcrprobe.so`
    已更新为 `fe358c0f`。

## 8. 风险

| 风险 | 对策 |
|---|---|
| 双方永远等待卡死 | 圣水满 + 最大等待兜底事件（§4.2/4.3） |
| 高维延迟头难学 | 邻域平滑 + 预训练初始化 |
| 事件边界优势估计偏差 | γ^Δticks + 边界 bootstrap，先小规模验证 |
| 探针改动需重打包 APK 部署 48 引擎 | P4 最后做，P1-P3 先验证全部语义 |

## 9. 与现有训练的关系

- 沿用 1x 截断 + 公主塔势奖励画像（SEGMENT_REWARD_SPEC）。
- 三专精联盟（league_train）作为事件驱动后的训练宿主。
- 强制 ACT 降级为可关兜底开关，默认关闭，依赖 HF 预训练。
