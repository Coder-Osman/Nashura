# 原生引擎 TCP/JSON 协议与训练环境契约规范（wire-spec）

> 本文档是对 `firstlight_CR` 仓库中 Python 环境层与原生（ARM64 libg）游戏引擎之间 TCP/JSON 协议的**穷尽式抽取**，作为重写训练引擎（模拟器）的唯一事实来源。
> 所有主张均带 `文件:行号` 锚点（路径相对 `native_runner/`）。

---

## 目录

1. [传输层](#1-传输层)
2. [会话生命周期与命令一览](#2-会话生命周期与命令一览)
3. [遥测 Schema 全量字段](#3-遥测-schema-全量字段)
4. [Tick 语义](#4-tick-语义)
5. [ActionV1 / ObservationV1 完整编码](#5-actionv1--observationv1-完整编码)
6. [终局规则](#6-终局规则)
7. [未决/不确定点清单](#7-未决不确定点清单)

---

## 1. 传输层

### 1.1 端点

- 默认 host `127.0.0.1`，port `26789`，可由环境变量 `CR_CONTROL_HOST` / `CR_CONTROL_PORT` 覆盖（`cr_native_env.py:33-34`）。
- 客户端默认超时 `10.0` 秒（`NativeClashEnv.__init__`，`cr_native_env.py:2482`）；评估客户端使用 30 秒（`training/v4/evaluate.py:38`）。
- TCP，`socket.create_connection` + `TCP_NODELAY`（`cr_native_env.py:2537-2538`）。

### 1.2 报文分帧（framing）

**所有协议均为"一行一个 JSON"的文本行协议：**

- 请求：`command.encode("utf-8") + b"\n"`（`cr_native_env.py:2569, 2579`）。命令本身是 ASCII 动词 + 空格分隔参数，或 `动词 + 空格 + 紧凑 JSON`（如 `configure {...}`、`inject {...}`）。
- 响应：单行 JSON，以 `\n` 结尾（每连接一请求一响应）。
- 两种连接模式（`_request`，`cr_native_env.py:2560-2616`）：
  1. **一次性连接**（默认）：每条命令新建 TCP 连接，发送一行命令，随后 `recv(65536)` 循环读取直到对端关闭（EOF），整体按 UTF-8 JSON 解析（`cr_native_env.py:2577-2590`）。
  2. **持久会话**（可选）：先发 `session-v1\n` 握手；服务端回一行 JSON `{"ok":true,"session":"control-session.v1"}`，否则标记不支持并回退一次性模式（`cr_native_env.py:2532-2558`）。握手行上限 4096 字节（`cr_native_env.py:2542-2544`）。之后每条命令仍是一行请求一行响应（`reader.readline`），响应上限 `MAX_RUNNER_RESPONSE_BYTES = 16 MiB`（`cr_native_env.py:56, 2570-2575`）。
- **请求/响应配对**：严格串行、同步、单线程锁（`threading.RLock`，`cr_native_env.py:2488-2489, 2561`）。没有命令 ID / 关联字段 —— 配对由"同一连接上按序一来一回"保证。
- 只读命令（`status`, `multi-status`, `observe`, `observe-rich`, `observe-atomic`, `render status`, `touch status`, `attest`）在传输/解析失败时最多自动重试 2 次，间隔 2 ms（`_read_only_request`，`cr_native_env.py:2497-2508, 2595-2606`）。

### 1.3 响应信封

- 每个响应均为 JSON 对象，必须含 `"ok": true`；否则读取 `error`（可附 `stage`/`bytesUsed`/`capacity` 诊断）并抛 `RunnerError`（`cr_native_env.py:2608-2615`）。
- 大多数响应附带头 `"tick"`（当前原生 tick）、`"generation"`、`"stateEpoch"`（见 §3 各 schema 校验）。

### 1.4 证明（attestation）字段

`attest` 命令返回进程身份 `RunnerAttestationV1`（`contracts.py:211-317`；`cr_native_env.py:2628-2638`）。字段：

| 字段 | 类型 | 语义 |
|---|---|---|
| `protocol_version` | str | 必须为 `cr-native-control.v1`（`contracts.py:38, 241-242`） |
| `package_name`, `abi` | str | 非空进程标识（`contracts.py:243-244`） |
| `probe_sha256`, `libg_sha256`, `content_fingerprint_sha256`, `content_assets_sha256` | 64 位小写 hex 或空 | 注入探针 / libg.so / 内容指纹（`contracts.py:245-248`） |
| `libg_build_id` | 40 位小写 hex 或空 | 20 字节 build id（`contracts.py:249-250`） |
| `content_version`, `content_manifest_sha1` | str / 40 位 hex | 内容版本与 manifest SHA-1（`contracts.py:229, 251-252`） |
| `hook_install_ok` | bool | 全部 hook 安装成功 |
| `hooks` | map[str,bool] | 恰好覆盖 `game_state_load, game_state_step, native_replay_runner, render_gate, replay_json_setter`（`contracts.py:201, 255-256`） |
| `capabilities` | map[str,bool] | 恰好覆盖 `headless, native_render, snapshot_restore, structured_observation, two_sided_actions`（`contracts.py:202, 257-258`） |
| `content_ready`, `content_assets_mapped` | bool | 运行时门控 |
| `production_ready` | bool | 必须等于"全部 readiness/hook/capability/哈希非空"的合取（`contracts.py:262-282`） |
| `attestation_digest` | 64 位 hex | `content_hash(identity_dict())`，identity 字段集见 `contracts.py:291-310` |

### 1.5 多住留（resident）寻址

`ResidentNativeClashEnv`（`cr_native_env.py:3205-3276`）：同一 host:port 上最多 16 个 `env_id ∈ 0..15`（`cr_native_env.py:3220-3221`）。除 `attest`、`touch *`、`render *`、`env {id} *` 外，所有命令自动加前缀 `env {env_id} `（`cr_native_env.py:3229-3243`）。住留模式下事件环在切换槽位时重启一个干净遥测纪元（`resident_event_rings_rebind_on_switch = True`，`cr_native_env.py:3213-3217`）。

---

## 2. 会话生命周期与命令一览

命令全集（均以一行文本发出，`_request(cmd)`；参数以空格分隔）：

| 命令 | 格式 | 语义 | 锚点 |
|---|---|---|---|
| `status` | `status` | 读 runner 状态；含 `coldReady`/`ready`/`mode`/`tick`/`queuedCommands` 等 | `cr_native_env.py:2618-2619, 2648-2663` |
| `multi-status` | `multi-status` | 住留集群状态 | `cr_native_env.py:3257-3258` |
| `multi-stop` | `multi-stop` | 销毁全部住留槽，恢复单例命令 | `cr_native_env.py:2621-2624` |
| `multi-perf` / `multi-perf-reset` | | 住留热路径计时计数器读/清零 | `cr_native_env.py:3260-3268` |
| `attest` | `attest` | §1.4 进程身份 | `cr_native_env.py:2628-2638` |
| `render on/off`, `render status` | | 本进程 EGL 帧提交开关 | `cr_native_env.py:2640-2646` |
| `configure {json}` | 载荷须为单行紧凑 JSON 对象、< 64 KiB | 安装全新战斗 schema 并同步 reset（命令/事件数组为空） | `cr_native_env.py:2665-2677` |
| `configure-native {json}` | 同上 | 用真实渲染器 ReplayBattleController 建场景；响应含 `sequence`，随后轮询 `status` 直到 `mode=="native-render" && nativeRenderReady && nativeRenderLoaded>=sequence` | `cr_native_env.py:2679-2705` |
| `step {n}` | `1<=n<=1_000_000` | 前进精确 n 个逻辑 tick，返回观察 | `cr_native_env.py:2739-2742` |
| `advance-native {n}` | 同上 | native-render 模式前进 n tick 后暂停；响应须满足 `mode=="native-render" && paused==true && requestedTicks==n` | `cr_native_env.py:2744-2758` |
| `observe` | | 普通（ordinary）引擎形态观察 | `cr_native_env.py:2760-2761` |
| `observe-rich` | | 富遥测信封（§3.1），经 `_validate_rich_observation` 校验 | `cr_native_env.py:2763-2771` |
| `observe-atomic` | | 同一管理器锁内同时捕获 ordinary+rich；经 `_validate_atomic_observation` 校验 | `cr_native_env.py:2773-2797` |
| `speed {v}` | v ∈ {0.25,0.5,1,2,4} | native-render 逻辑速度 | `cr_native_env.py:2799-2811` |
| `touch status` | | 原生屏幕选牌状态 | `cr_native_env.py:2813-2816` |
| `pause` / `resume` | | native-render 暂停/恢复 | `cr_native_env.py:2818-2826` |
| `snapshot-create` | | 进程内无损状态快照，返回 uint64 `handle`；`reset`/`create_match` 使其失效 | `cr_native_env.py:2835-2843` |
| `restore {handle}` | | 原地恢复快照并返回观察 | `cr_native_env.py:2845-2853` |
| `release-snapshot {handle}` | | 释放快照 | `cr_native_env.py:2855-2859` |
| `inject {json}` | 载荷单行紧凑 JSON、< 2 KiB | 入队一条原生逻辑命令（不推进时间） | `cr_native_env.py:2861-2871` |
| `activate-ability {owner} {object_index} {secondary_index} {execute_in_ticks}` | owner∈{0,1}；身份为 tagged entity key `[owner,-2,nativeObjectId]` 或 legacy 非负二元组；`1<=execute_in<=65535` | 排队一个就绪冠军技能；响应含 `tick`（排队时刻），执行时刻 = tick + execute_in | `cr_native_env.py:2958-2981` |
| `deploy {player_id} {card_id} {card_parameter} {x} {y} {delay_ticks}` | | 入队一条已编码命令（低层） | `cr_native_env.py:3074-3081` |
| `play {delay} {advance} {n} {owner hand_index x y}×n` | n<=8 | 组合：排 n 个手牌动作并推进（`transition`） | `cr_native_env.py:3083-3126` |
| `replay-schedule-card {owner} {card_id} {x} {y} {execute_tick}` | x∈[0,18000), y∈[0,32000)（千分之一格单位），`1<=tick<=0x7FFFFFFF` | 注册 probe 拥有的渲染回放出牌，在 `execute_tick-1` 边界解析；响应 `sequence`、`registeredAtTick` | `cr_native_env.py:2988-3018` |
| `replay-schedule-ability {owner} {hints|-} {execute_tick}` | hints ≤260 字符 | 注册回放技能（按名称提示解析唯一可排队冠军） | `cr_native_env.py:3020-3051` |
| `replay-schedule-status {sequence}` | | 状态 ∈ {pending, succeeded, failed}，含 `executeTick` | `cr_native_env.py:3053-3067` |
| `replay-schedule-clear` | | 丢弃所有 probe 拥有的语义动作 | `cr_native_env.py:2983-2986` |
| `env {id} close` | | 关闭一个住留槽 | `cr_native_env.py:3270-3276` |

### 2.1 会话建立（训练客户端视角，`training/v4/evaluate.py`）

1. 构造 `NativeClashEnv(host, port, timeout=30)`；设置 `native.public_card_play_events_from_combat_ring = True`（`evaluate.py:38-39`）。
2. 构造 `BattleEnvV1(native=..., card_catalog=..., semantic_subset_contract=..., effect_catalog=...)`（`evaluate.py:40-45`）。
3. `environment.reset(match_config=..., options={"render_mode":"headless","decision_ticks":5,"event_driven_decisions":False,"allow_initial_remaining_runtime_rejections":True})`（`evaluate.py:74-82`）。reset 内部：校验规则集 manifest → 校验 deck 卡全部有 CardSpec → `attest` 验证 → `native.create_match(match)`（即 `configure` + `observe` + 双方 deck 校验，`cr_native_env.py:2707-2720`）→ headless 预热 `step(warmup_ticks)`（默认 `warmup_ticks = FIRST_PLAYABLE_TICK = 90`，`battle_env.py:382`）→ `observe-atomic` 捕获初始状态（`battle_env.py:884-908` 附近）。
4. 决策循环：双方在 `observation.tick >= 90` 后每 `POLICY_DECISION_TICKS = 5` tick 出一次动作（`evaluate.py:107-112`；`training/v4/expert.py:26-27`）。空决策用 `ActionV1.wait(owner, ticks=5)`。
5. `environment.step(commands)` 返回 `(observations, rewards, terminations, truncations, infos)`（PettingZoo 并行签名，`battle_env.py:2335-2342`）。
6. 结束后读取 `observations[0].terminal`；`native.close_transport()`（`evaluate.py:119-152`）。

### 2.2 reset 时的关键选项（`battle_env.py:645-778`）

`render_mode`（headless|native-render）、`decision_ticks`（转为 `decision_hz = 20/decision_ticks`）、`event_driven_decisions`（布尔）、`allow_initial_remaining_runtime_rejections`、`defer_headless_warmup`、`headless_initial_step_ticks`(0/1)、`pause_native_render`、`suppress_native_render`、`synchronize_native_render_steps`、`native_render_speed`、`reuse_runner_attestation`、`allow_verified_standard_layout_alias`。
reset 强制：`match.end_tick == max_battle_ticks == 7200`（`battle_env.py:799-803`）；双方 tower_troop id 必须在竞技池 `{159000000 Tower Princess, 159000001 Cannoneer, 159000002 Dagger Duchess, 159000004 Royal Chef}`（`battle_env.py:804-807`；`contracts.py:60-67`）；episode 时钟起点固定为原生 tick 0（`_episode_start_native_tick = 0`，`battle_env.py:874-876`）。

### 2.3 MatchConfig（`match_factory.py:86-124`）

`deck0/deck1`（各 8 张正整数卡 ID）、`deck0_form_availability/deck1_form_availability`（8 元 form 掩码，0=基础）、`tower_troop0_id/tower_troop1_id`（默认 159000000）、`seed`（32 位）、`game_mode=72000006`、`arena=54000001`、`location=15000199`、`level_cap=0`、`minimum_card_level=0`、`king_tower_level`、`owner0/1_name`、固定 `owner0_account_id=1`、`owner1_account_id=2`（`match_factory.py:20-21`）、`end_tick=7200`。`to_json` 基于 `data/native_match.json` 模板填充并**删除录制的 commands/events**（`match_factory.py:171-238`）。

---

## 3. 遥测 Schema 全量字段

所有事件环形缓冲（ring）信封共用字段集 `_EVENT_ENVELOPE_FIELDS`（`cr_native_env.py:668-684`）：
`ok, schema, generation, stateEpoch, observationTick, capacity, hookSetAttested, capability, epochFirstSequence, oldestRetainedSequence, nextSequence, overflowCount, sequenceGapBeforeOldest, complete, events`（部分附加 `transmitFromSequence`）。

通用 ring 不变量（`_check_event_header` / `_check_event_window` / `_check_event_list`，`cr_native_env.py:687-739`）：
- `generation/stateEpoch/observationTick` 必须与所属富遥测身份一致；`capacity` 为常量。
- `overflowCount == max(0, nextSequence - epochFirstSequence - capacity)`；
- `oldestRetainedSequence == max(epochFirstSequence, nextSequence - capacity)`；
- `sequenceGapBeforeOldest == (oldest > epochFirst)`；
- `events` 序列号从 `transmitFromSequence`（缺省 oldest）起连续；数量必须等于 `nextSequence - transmitFrom`。
- `capability` 记录字段：`status ∈ {authoritative, derived, pending, unavailable}`（`cr_native_env.py:52`）、非空 `source`/`validation`/`confidence`、`failClosed: bool`（`cr_native_env.py:299-307`）。

### 3.1 native-rich-telemetry.v3（`observe-rich` / `observe-atomic.rich`）

顶层字段（`cr_native_env.py:1945-1980`）：
`ok, schema="native-rich-telemetry.v3", statusEnum(=四状态枚举), generation, stateEpoch, tick, provenance(map), capabilities(map), count, objects[], players[2], combatEvents, returned, truncated` + 可选 `phaseRuntime, specialMovementRuntime, actionMovementRuntime, characterStateRuntime, visibilityRuntime, remainingRuntime, towerTroopRuntime`。

- `capabilities` 必含 22 项且状态固定：`entityCore/targetCoordinates/targetEntity/hitpoints/componentInventory/shield/attackSequenceStage/buffs/invisibility = authoritative`；`slow/rage/stun/freeze/attackPhase/deployPhase/chargeStage = derived`(有 phaseRuntime 时) 或 `unavailable`；`debuffs/sourceEntity/impact/visibility = unavailable`；`projectile/abilityRuntime/evolutionRuntime = derived`（`cr_native_env.py:1996-2051`）。
- `provenance` 必含约 40 个键且状态固定（如 `nativeObjectId/dataGlobalId/targetEntityKey/shield.*/activeEffects.*/invisibleCount/components.slots.* = authoritative` 等，`cr_native_env.py:2052-2098`）。
- `count/returned/truncated/objects`：`len(objects)==returned<=count<=256`；非截断时 `returned==count`（`cr_native_env.py:2100-2107`；`MAX_OBSERVATION_OBJECTS=256`，`cr_native_env.py:53`）。

**objects[i]（活动对象，v3）** 字段（`cr_native_env.py:2124-2158`）：
`slot(连续), nativeObjectId(uint32, 唯一), entityKey=[owner,-2,nativeObjectId]`（`NATIVE_OBJECT_ID_ENTITY_KEY_TAG=-2`，`cr_native_env.py:55, 292-296`）`, owner, cardId, dataGlobalId, x, y, targetX, targetY`（整数，千分之一格世界坐标）`, targetEntityKey|null, targetEntityValidated(bool), objectIndex, secondaryIndex, hp|null, maxHp|null, shield{current,max,status=authoritative}|null, attackSequenceStage(0..255)|null, invisibleCount|None, visibilityState("visible"/"invisible" 由 count 推出), projectile|null, entityResourceRuntime|null, periodicAttackModifierRuntime|null, captureRuntime|null, thresholdRelocationRuntime|null, activeEffects[<=64]|null, components{...}` + 可选 `phaseRuntime`（有 ring 时）与 `towerTroopRuntime`。
空槽：`{"slot":i,"null":true}`（`cr_native_env.py:2119-2122`）。

- `activeEffects[k]`：`{buffGlobalId(uint32), name(1..128 UTF-8 字节), remainingMs(>=-1), sourceEntityKey|null, sourceEntityValidated(bool)}`（`cr_native_env.py:2229-2261`）。
- `components`：`{valid:bool, count, capacity<=64, slots[]}`；每 slot `{slot, present, ownerBacklinkValid|null, vtableLibgOffset|null, typeGetterLibgOffset|null, engineType(0..31)|null, typeValid, slotMatchesType|null}`（`cr_native_env.py:2300-2363`）。
- `projectile`（每对象至多 1 个，v2）：`{projectileDataGlobalId, sourceEntityKey, sourceEntityValidated, targetEntityKey, targetEntityValidated, homingTargetEntityKey, homingTargetEntityValidated, destinationX, destinationY, terminal(bool), nativePhase("in_flight"|"terminal_or_finished_processing"), dragStage(null|"outbound"|"drag_back_active")}`（`cr_native_env.py:322-371`）。
- `entityResourceRuntime`：`{kind="extra_spawn_accumulator", currentRaw, capacityRaw(>=1), baseRaw, limitRaw, normalized, status="authoritative"}`，约束 `limit-base==capacity`、`normalized==current/capacity`（`cr_native_env.py:374-400`）。
- `periodicAttackModifierRuntime`（`native-periodic-attack-modifier-runtime.v1`）：`{schema, actionDataGlobalId, phase("source_alive"|"source_death_linger"), periodAttacks, completedAttacks(<period), addedDamageRaw, lingerDurationMs, lingerRemainingMs, sourceNativeObjectId, sourceEntityKey|null, sourceResolved(bool), status="authoritative"}`（`cr_native_env.py:403-453`）。
- `captureRuntime`（`native-capture-runtime.v1`）：`{schema, actionDataGlobalId, phase("active"|"release_cooldown"|"idle" 由 targets/cooldown 推出), dragDelayMs, grabPauseMs, captureDragTimeMs, configuredCooldownMs, cooldownRemainingMs, hitFrequencyMs, hitAccumulatorMs, completionResultCurrentUpdate(bool), firstCaptureHandled(bool), targets[<=160], status}`；target：`{targetNativeObjectId, targetEntityKey|null, targetResolved(bool), phase("acquired_delay"|"grab_pause"|"dragging"|"contained"|"release_pending"), elapsedMs, phaseBudgetRemainingMs|null}`，阶段预算满足 `remaining == max(边界-elapsed, 0)`（`cr_native_env.py:456-550`）。
- `thresholdRelocationRuntime`（`native-threshold-relocation-runtime.v1`）：`{schema, actionDataGlobalId, phase("waiting_threshold"|"relocating"|"exhausted"), stage(1..2N+1), relocationIndex(=(stage-1)//2), hideDurationMs, remainingMs, burrowed(bool), thresholdsPercent[1..16 严格降序 0..100], status}`（`cr_native_env.py:553-600`）。

**players[i]（v2）**：`{owner, ownerRootValidated(bool), ownerEntityKey|null, abilityRuntime|null, evolutionRuntime|null}`（`cr_native_env.py:1799-1806`）。
- `abilityRuntime[k]`（<=2 个 controller）：`{controllerSlot(1..2 严格递增), actionDataGlobalId, actionDataName(1..128B), selectedCharacterDataGlobalId, remainingCooldownMs<=configuredCooldownMs, configuredCooldownMs, remainingChargesRaw(-1 无电荷或 0..maxCharges), maxCharges, buttonState(int), buttonStateLabel, available(=buttonState∈{2,4}), championEntityKeys[[owner,-2,id]...]}`（`cr_native_env.py:1830-1892`）。buttonState 枚举 14 项见 `ABILITY_BUTTON_STATE_LABELS`（`cr_native_env.py:142-158`），可排队状态 = {2 "Ready", 4 "ChampionDeploying"}（`cr_native_env.py:159`）。
- `evolutionRuntime[k]`（<=8，deckSlot 连续）：`{deckSlot, cardId, baseSpellGlobalId(==cardId), evolvable(bool), evolutionFormGlobalId|null, progress, cycleRequired, cycleRemaining(==cycleRequired-progress), ready(==progress>=cycleRequired)}`；不可进化卡只暴露前四字段且 progress=0（`cr_native_env.py:1894-1942`）。

### 3.2 native-observation-capture.v1（`observe-atomic` 顶层）

字段（`_validate_atomic_observation`，`cr_native_env.py:2388-2440`）：
`schema="native-observation-capture.v1", ok=true, atomic=true, maxObjects(==256), tick, generation, stateEpoch, ordinary{...}, rich{...}`。
- `ordinary`/`rich` 内嵌信封均有 `ok/tick/generation/stateEpoch` 且三者与外层一致；ordinary 含 `count/returned/truncated/objects[]`（slot 连续）。
- rich.count == ordinary.count、rich.returned == ordinary.returned；任何一侧截断即失败。这是"同一管理器同一状态"的原子性证明。

### 3.3 native-combat-events.v1

信封字段 = ring 公共字段 + `rejectedCaptureCount, hookSetInstalled`（+可选 `transmitFromSequence`）；capacity 1024（`cr_native_env.py:64, 742-770`）。
事件字段（`cr_native_env.py:793-810`）：
`sequence, tick(>=-1), generation, stateEpoch, kind, hookOffset, callerOffset|null, causeSequence|null, pool, terminalReason, lethal(bool), deploymentContext|null, target, immediateSource, source, projectile, related, routeSourceBefore, routeTargetBefore, routeSourceAfter, routeTargetAfter`（9 个实体 fact）`, requestedAmount|null, actualAmount|null, preHp, postHp, preBuiltInShield, postBuiltInShield, preBuffShield, postBuffShield`（8 个数量）`, destinationBefore|null, destinationAfter|null`（[x,y] 整数）+ 可选 `spawnProvenance`。

- `kind` 13 种：`damage, death, heal, spawn, projectile_spawn, despawn, shield_damage, shield_break, projectile_impact, projectile_deflect, projectile_expire, projectile_terminal, card_play`（`cr_native_env.py:106-122`）；每种绑定允许的 `hookOffset`（`COMBAT_HOOK_OFFSETS`，`cr_native_env.py:126-140`）与部分 `callerOffset` 白名单（`cr_native_env.py:866-883`）。
- `pool ∈ {none, hitpoints, built_in_shield, buff_shield}`；`terminalReason ∈ {none, object_impact, source_lost, nonimpact_cancel, unknown}`（`cr_native_env.py:123-124`）。
- 实体 fact 字段：`{validated, present, nativeObjectId, entityKey, owner, objectIndex, secondaryIndex, cardId, objectKind(-1..31), position[x,y], visibilityValidated, invisibleCount|null}`；absent 时除校验位外全 null（`cr_native_env.py:603-665`）。
- 语义约束（要点）：amount 类事件须 `actualAmount>0` 且 pool 前后差 == actualAmount；`lethal` 仅 damage/death 且 postHp==0；`death` 必须链接 causeSequence 的 lethal damage；`shield_break` 终值为 0；`card_play` 必带 deploymentContext；cause 链 kind 匹配表见 `cr_native_env.py:1113-1135`（`cr_native_env.py:742-1136` 全量）。
- `deploymentContext`：`{deploymentSequence, owner(0/1), playedCardGlobalId, effectiveCardGlobalId|null, cardParameter(uint32 打包), deckSlot(0..7), cost(0..15), formCode, formName, consumeHookOffset(==0xF38A68)}`；Mirror（28000006）在 BasicForm 下 effective≠played（`cr_native_env.py:894-984`；`MIRROR_CARD_GLOBAL_ID`，`cr_native_env.py:141`）。
- `cardParameter` 位打包（RE 证明）：bits0-3 = form_code（`CARD_FORM_LABELS = BasicForm/EvoForm/HeroForm/AutoChessForm/FormEnd/FlexSlot`，`cr_native_env.py:160`）；bits22-27 = deck_slot+1；bits28-31 = elixir cost（`cr_native_env.py:183-212`）。
- `spawnProvenance`：`{kind("area_tick_nested_spawn"@0xF143A8 | "action_spawn_to_location"@0xE7B864), hookOffset, sourceDataGlobalId}`（`cr_native_env.py:986-1016`）。

### 3.4 native-phase-runtime.v1

信封 = ring 公共字段 + `hookSetInstalled, rejectedCount`；capacity 4096（`cr_native_env.py:65, 1281-1310`）。
事件字段（`cr_native_env.py:1312-1332`）：
`sequence, tick(>=0), kind, hookOffset, callerOffset|null, entity(fact), targetBefore|null, targetAfter|null, buffGlobalId|null, buffRemainingMs|null(>=-1), speedMultiplier|null, hitSpeedMultiplier|null, inputStep|null, outputStep|null, timelineBefore|null, timelineAfter|null, classicChargeBefore|null(>=-1), classicChargeAfter|null, success(bool)`。
`kind` 11 种且 hookOffset 一一对应（`PHASE_EVENT_HOOKS`，`cr_native_env.py:68-82`）：`attack_start@0xF23110, attack_release@0xF5F100, effect_apply@0xF5A2D4, attack_scale@0xF5B4AC, movement_scale@0xF5B3C8, effective_movement_speed@0xF1D004, movement_delta@0xF68808, target_reset@0xF5C894, classic_charge_ready@0xF23110(调用方 0xF68958 区分), deploy_scale@0xF5B3C8`。

**phaseObject（对象级快照，native-phase-object.v1）**（`cr_native_env.py:1139-1278`）：
字段 `schema, attackValidated, movementValidated, buffsValidated, attackSequenceStage(-1..255)|null, attackTimelineMs, loadRemainingMs, deployRemainingMs, deployPreviousMs, configuredDeployTimeMs, hitSpeedMs, attackDashTimeMs, baseMovementSpeed, chargeSpeedMultiplier, classicChargeProgress(>=-1), speedPositivePercent, speedNegativeMagnitude, hitSpeedPositivePercent, hitSpeedNegativeMagnitude, attackStepInput/Output/Tick, movementStepInput/Output/Tick, deployStepInput/Output/Tick, effectiveMovementSpeed/Tick, movementDelta/Tick` + 可选 `attackSequenceProgressRaw(0..50), attackSequenceProgressLimit(==50), attackSequenceDecayRemainingMs(<=7000), attackSequenceDecayDurationMs(==7000)`。
关键派生：`attackSequenceProgress p → stage = 0 if p<4, 1 if p<9, 2 if p<49, 3`（`cr_native_env.py:1251-1254`）；缓存值/tick 成对（tick=-1 ⟺ 值 null）；`movementValidated ⟺ classicChargeProgress 存在`；buffsValidated ⟺ 四个 buff 极值字段全存在。

### 3.5 native-visibility-runtime.v1

信封 = ring 公共字段 + `ownerRelativeVisibility, transitionHookSetInstalled, contextualGateHookSetInstalled(必须 false), contextualGateCapability, rejectedCount`；capacity 4096（`cr_native_env.py:66, 1409-1466`）。
`ownerRelativeVisibility = {publicByOwner:{status:"unavailable",reason:"no-exact-owner-conditioned-native-producer"}, targetableByOwner:{同}}` —— owner 相对可见性**没有**原生生产者，fail-closed（`cr_native_env.py:1445-1455`；`visibility_runtime.py:331-343`）。
事件字段（`cr_native_env.py:1468-1480`）：`sequence, tick, kind, hookOffset, callerOffset, subject(fact, 必须 present+visibilityValidated), buffGlobalId, invisibleCountBefore, invisibleCountAfter, scope="native_invisibility_phase", completeContext(true)`。
`kind ∈ {became_invisible@0xF5A2D4(caller 0xF59EAC), became_visible@0xF5A578(caller 0xF59308)}`；before/after 必须精确跨越 (0,1)/(1,0) 边界；subject.invisibleCount == after（`cr_native_env.py:1492-1515`）。源模块 `visibility_runtime.py` 还定义了 Royal Ghost（26000050）、Buff/Attack 组件 vtable、两个攻击选择器 gate offset（`visibility_runtime.py:29-41`）。

### 3.6 native-remaining-runtime.v1

信封 = ring 公共字段 + `ownerRelativeVisibility(同上 fail-closed), hookSetInstalled, rejectedCount`；capacity 4096（`cr_native_env.py:67, 1519-1563`）。
事件字段（30 个，`cr_native_env.py:1565-1597`）：
`sequence, tick, kind, hookOffset, callerOffset|null, entity, source, target, targetBefore, targetAfter（fact）, objectKind|null, runtimeVtableOffset|null, dataBeforeGlobalId, dataAfterGlobalId, expectedCharacterDataGlobalId, expectedProjectileDataGlobalId, configuredDataGlobalId, resourcePreFixed, resourcePostFixed, resourceActualDeltaFixed, amountArgument, configuredAmountArgument, resourceOwner, areaRemainingLifeMs, resourceCause("none"|"periodic"|"death_owner"|"death_opponent"), transformKind("none"|"character"|"projectile"), result(bool), option(bool), committed(bool), resetTarget(bool), completeContext(true)`。
`kind` 集合及 hook 绑定见 `REMAINING_EVENT_HOOKS`（`cr_native_env.py:85-105`）：`area_create/area_expire@0xF25B8C/0xF25D90, resource_delta@{0xF3BA4C,0xF3B3D4}, tower_aggro_*/tower_activate@{0xF5C894,0xF23110}, lifetime_spawn/despawn, transform@0xE58638, spawn_attach@0xF19314, area_damage_eligibility@0xF1CB90, area_relation_* , area_action_spawn@{0xF143A8,0xE7B864}`。
各 kind 的边校验（fact 存在性、objectKind==3、vtable 0x189C2E8、caller 白名单、resource 算术 `actual==post-pre<=amount*scale`、tower 激活 caller 0xF58820 等）见 `cr_native_env.py:1598-1796`。

### 3.7 其余 ring schema

- **native-special-movement-runtime.v1**（`special_movement_runtime.py`）：capacity 1024；唯一事件 `kind="ordinary_dash_execute", mode="dash", stage="execute", hookOffset=0xF609DC, callerOffset=0xF61BF0`；字段含 `sourceBefore/sourceAfter/target`（fact）、`requestedX/Y, targetRadius, operationFlag(==1), cooldownBeforeMs/AfterMs, positionChanged(==坐标变化), completeContext`（`special_movement_runtime.py:56-177`）。
- **native-action-movement-runtime.v1**（`action_movement_runtime.py`）：capacity 1024；事件 kind 白名单 3 种：`golden_knight_chain_hop_launch`（GoldenKnight 26000074, hook 0xF49D24, chainIndex 必填）、`boss_bandit_warp_position_commit`（260000103, hook 0xE82A34, 位置必须变化）、`elite_archer_warp_position_commit`（{26000062,20300062}）；字段 `mode, stage, actionDataGlobalId, actionName, actionClassVtableOffset, runtimeClassVtableOffset|null, sourceBefore/After, target, chainIndex|null, positionChanged, completeContext`（`action_movement_runtime.py:115-244`）。
- **native-character-state-runtime.v1**（`character_state_runtime.py`）：capacity 2048；唯一 `kind="native_character_state_transition"`，hook 0xF18654；字段 `previousState/requestedState/committedState(0..16, requested==committed 且 !=previous), sourceBefore/After, positionChanged, completeContext`；源卡白名单 9 张（Bandit/MegaKnight/EliteArcher/GoldenKnight/SuperHogRiderTerry/LittlePrince/BossBandit/ChampionGuard，`character_state_runtime.py:22-34`）。
- **towerTroopRuntime**（rich 顶层可选）：见 `contracts.py` 的 `dagger-duchess-runtime.v1`（`charge_count, max_charge_count, recharge_elapsed_ms, recharge_duration_ms`，`contracts.py:1866-1891`）与 `royal-chef-runtime.v1`（`start_delay_remaining_ms, start_delay_duration_ms, cooking_contribution, contribution_needed, surviving_side_towers(0..2), throw_delay_remaining_ms|null, target_entity|null`，`contracts.py:1894-1938`）。

### 3.8 普通观察（`observe` 的 ordinary 形态）

Python 侧直接消费的字段（散见 battle_env）：顶层 `tick, players[], objects[], ended/finalized/finalizedEnded, winner/resultOwner, crownsRaw|crowns, queuedCommands`；`players[i]`：`owner, accountId, elixirRaw(定点 1/10000), elixir, crowns(Raw), hand[{handIndex, cardId, commandCardId|null, cardParameter|null, cost, deckSlot?}], deck[{cardId}], nextCard`（如 `cr_native_env.py:2713-2716, 2904-2912, 3150-3158`；`contracts.py:1826-1862` 的 elixirRaw 语义）。**注意：ordinary 的完整 schema 由原生 probe 产生，Python 只做部分校验 —— 重写时须以行为快照对齐（见 §7）。**

---

## 4. Tick 语义

### 4.1 常量

| 常量 | 值 | 锚点 |
|---|---|---|
| `TICKS_PER_SECOND` | 20（`TICK_MS=50`） | `battle_env.py:115-116`；`contracts.py:59` |
| `FIRST_PLAYABLE_TICK` | 90（4.5 s 预热；也是默认 `warmup_ticks` 与 `FIRST_POLICY_DECISION_TICK`） | `cr_native_env.py:36`；`battle_env.py:382`；`expert.py:26` |
| `POLICY_DECISION_TICKS` | 5（决策步长 = 250 ms，`decision_hz=4`… 实为 20/5=4 Hz） | `expert.py:27`；`evaluate.py:78` |
| `LIVE_COMMAND_AGE_TICKS` | 20（live 命令 t/t2 年龄区间） | `cr_native_env.py:37` |
| `COMMAND_CONSUMPTION_STEPS` | 21（=20+1，跨消费边界所需步数） | `cr_native_env.py:41` |
| `NATIVE_GAMEPLAY_END_TICK` | 6000（180 s 常规 + 120 s 加时 @20Hz，此后无合法决策） | `match_factory.py:24` |
| `NATIVE_FINALIZATION_DEADLINE_TICK` / `NATIVE_MATCH_END_TICK` | 7200（收尾看门狗，非额外游戏时间） | `match_factory.py:28-31` |
| 圣水速率表（分段，raw/整条 10 圣水所需 ms） | tick 0-2400: 28000ms；2400-4800: 14000ms；4800-6000: 9300ms | `battle_env.py:131-135` |

### 4.2 决策节奏

- `BattleEnvV1` 为每个 owner 维护 `_decision_deadline_native`（原生 tick）；`step()` 中若 `before_tick < deadline`，任何非 WAIT 动作直接抛错，并被替换为合成 WAIT（`battle_env.py:2371-2384`）。
- 出现动作后 `deadline = before_tick + max(action.next_decision_ticks …)`（WAIT_UNTIL_AFFORDABLE 用 `mode_timeline.ticks_for_raw` 精确到圣水缺口，`battle_env.py:2289-2298, 2428-2434`）。
- 实际推进 `advance_ticks = max(1, min(两方 deadline) - before_tick)`（`battle_env.py:2437`）。
- reset 时 `deadline = 当前 tick`（即 reset 后双方立即可决策；headless 已预热到 90）。
- 评估路径固定节奏：`decision_ticks=5`、`event_driven_decisions=False`（`evaluate.py:78-80`）。

### 4.3 部署时机与命令编码（ct=86）

手牌动作通过 `inject` 的原生命令（`cr_native_env.py:2913-2925`）：
```json
{"ct":86,"c":{"t":boundary-20,"t2":boundary,"idHi":accountId>>32,"idLo":accountId&0xFFFFFFFF,
  "px":x,"py":y,"sid":-1,"sel":{"os":commandCardId,"pd":cardParameter}}}
```
- `boundary = applied_tick - 1`（引擎在从 t2 推进到下一状态时消费命令；`applied_tick >= FIRST_PLAYABLE_TICK+1`，`cr_native_env.py:2894-2902`）。
- `x,y` 为千分之一格世界坐标（0..17999 / 0..31999）；`cell_to_world` + `subcell_offset*1000`，最终 clamp（`battle_env.py:2281-2287`）。
- 部署到执行：排队的 `executeTick = boundary+1`；等待中的 pending 直到圣水/手牌轮转实际消费才结算（`_resolve_pending`）。
- `transition`/`play`：`delay=None` 时自动选 `max(1, 90-current_tick)`，固定帧跳过时 `advance >= delay + 21`（`cr_native_env.py:3111-3126`）。
- 可恢复错误：step 返回 `"native hand card is invalid"` 时最多重试 21 次（不重注入命令）（`cr_native_env.py:3174-3200`）。
- 技能激活是独立的 `activate-ability` 命令（ct 语义对齐原生 ct=2，无目标载荷，`battle_env.py:2129-2131`）。

### 4.4 合法性与拒绝语义

**动作合法性（Python 门，`_validate_action`/`action_mask`，`battle_env.py:1938-2279`）：**
- 未到决策 deadline：只有 WAIT 合法（mask `decision_ready:false`）。
- 手牌槽非法原因：`not_in_hand / pending_execution / native_selection_unavailable(cardParameter 或 cost 缺失) / insufficient_elixir(cost*10000 > effective_elixir_raw) / card_spec_unavailable / no_legal_placement`（`battle_env.py:2003-2066`）。圣水预留：pending 卡+技能费用从 `elixirRaw` 中扣除（`battle_env.py:1964-1967`）。
- 放置合法性：32×18 row-major 布尔 mask（`placement_masks[str(slot)].row_major[y][x]`）+ 最终世界坐标复核 `card_placement_legal_world`（含领地规则、被毁公主塔开放通道 `destroyed_enemy_princess_lanes`、活动塔脚印、占用建筑脚印）（`battle_env.py:2062-2064, 2202-2211`）。
- 建筑卡：必须携带与 owner 镜像符号一致的精确 `subcell_offset`（`model_subcell_offset * ±1`），且存在未验证占用建筑即拒绝（`battle_env.py:2178-2201`）。
- 复合动作（1..3 个/决策）：WAIT 不可混合；手槽不可重复；技能至多 1 次；建筑脚印不得互相重叠；总费用（含预留）不得超过 `elixirRaw`（`battle_env.py:2213-2279`）。
- 技能：source 必须在 `_ability_action_candidates` 中（就绪 buttonState、冷却 0、费用可负担、有完整原生 runtime 身份且与静态 spec 的 cooldown/charges 一致，否则 fail-closed）（`battle_env.py:2129-2143, 2439-2473`）。
- `execute_offset_ticks <= 0xFFFF`（`battle_env.py:2114-2115`）。

**拒绝路径**：非法动作在 Python 侧抛 `BattleEnvError`（step 前验证）；引擎侧拒绝体现为 pending 记录超时/取消并发出 `action_rejected` 事件（评估统计 `event.event_type == "action_rejected"`，`evaluate.py:115-118`）、`action_canceled_terminal`（终局取消，`battle_env.py:2531-2544`）。合法排队回执字段必须与宿主侧 runtime 契约完全一致否则抛错（`battle_env.py:2549-2555`）。

---

## 5. ActionV1 / ObservationV1 完整编码

### 5.1 坐标系

- 逻辑棋盘 `BOARD_WIDTH=18 × BOARD_HEIGHT=32`（列×行）（`contracts.py:57-58`）。
- 原生世界坐标 = 千分之一格，x∈[0,18000), y∈[0,32000)（`cr_native_env.py:3002-3003`；`battle_env.py:2287`）。
- 塔（owner0 在下）世界坐标：king(9000,3000)、princess_left(3500,6500)、princess_right(14500,6500)；owner1 镜像 y=29000/25500（`battle_env.py:138-145`）。
- `target_grid=(x,y)`，x∈[0,18), y∈[0,32)；`subcell_offset ∈ [-0.5,0.5]²`（`contracts.py:1656-1659`）。
- RasterV2：shape (32,18)（高×宽），`spatial_frame="native-arena-row-major"`（owner 视角可带 `perspective_transform_hash`）（`contracts.py:2473-2551`）。

### 5.2 ActionV1（version `action.v1`，`contracts.py:1614-1721`）

| 字段 | 类型 | 语义/约束 |
|---|---|---|
| `owner` | int | 0/1 |
| `kind` | enum | `wait / wait_until_affordable / play_card / activate_ability` |
| `hand_slot` | int? | 0..3；play_card 与 wait_until_affordable 必填 |
| `card_id` | int? | >0；须与手槽实际 cardId 一致（battle_env 校验） |
| `source_entity` | int? | activate_ability 必填（实体 id） |
| `ability_id` | str? | 可选一致性校验 |
| `target_kind` | enum | `none/grid/friendly_entity/enemy_entity/direction/area_center`；grid 类必带 target_grid，entity 类必带 target_entity |
| `target_grid` | [int,int]? | 棋盘格 |
| `target_entity` | int? | |
| `subcell_offset` | [float,float]? | [-0.5,0.5] |
| `execute_offset_ticks` | int | >=0, <=65535（battle_env 侧） |
| `next_decision_ticks` | int | >=1 |
| `requested_at_ms / generated_at_ms` | int? | 时延核算 |
| `action_id` | str? | |
| `metadata` | map | |
构造器：`ActionV1.wait(owner, ticks)`、`ActionV1.play(owner, hand_slot, grid)`（`contracts.py:1680-1693`）。`from_mapping` 接受 `target:{grid,entity}` 嵌套与若干别名（`candidate/source_entity_id/target_entity_id/target_offset`，`contracts.py:1696-1721`）。

### 5.3 ObservationV1（version `observation.v1`，`contracts.py:2663-2734`）

顶层：`tier(oracle|fair|human|rgb), tick, owner|null, time(TimeStateV1), phase(str), players[], towers[], entities[], events[], opponent_belief|null, action_mask, pending_actions[], terminal, native_digest|null, ruleset_id, episode_id, raster|null, truncated(bool), metadata`。
- 公平性铁律：tier != oracle 时必须 owner 非 null；`native_digest` 禁止出现；对手 `private_state_visible` 必须 false；`entity.internal`/`tower.internal` 不得泄漏；对手 pending 不得泄漏（`contracts.py:2694-2734`）。
- `TimeStateV1`：`elapsed_ms, remaining_ms, server_time_ms, capture_time_ms, available_time_ms, elixir_multiplier(>0), tick_ms(默认 50)`（`contracts.py:1724-1754`）。
- `PlayerStateV1`：`owner, crowns(0..3), tower_ids, elixir_exact(0..10), elixir_visible, hand(<=4), next_card, deck(<=8), cycle, revealed_cards, evolution_state, ability_state, private_state_visible, metadata, ability_runtime_states[], evolution_runtime_states[], runtime_provenance`；`elixirRaw>10` 时按 /10000 换算（`contracts.py:1763-1862`）。
- `TowerStateV1`：`entity_id, owner, tower_kind("king"/"princess_left"/"princess_right"), position, hitpoints<=max_hitpoints, tower_troop_id(competitive 池; king 仅可 Royal Chef 或 null), shield, active, visible_target, status, internal, shield_state?, effect_states[], attack_state?, visibility_state?, tower_troop_runtime?, runtime_provenance`（`contracts.py:1944-2047`）。
- `EntityStateV1`：`entity_id, owner, card_id, entity_kind, position, velocity, hitpoints(可超 max —— Evolution Witch 超疗), shield, age_ms, visible_target, movement_target, attack_phase, status, effects, source_entity, causal_group(CausalGroupRefV1{kind:deployment/spawn_wave/volley/persistent_effect, handle, source_card_id, parent_entity_id}), visible, native_data_global_id, internal, shield_state?, effect_states[], attack_state?, movement_runtime?(movement-runtime.v1), deployment_runtime?(deployment-runtime.v1), projectile_state?, visibility_state?, ability_states[], evolution_state?, resource_states[], capture_runtime?, threshold_relocation_runtime?, periodic_attack_modifier?, runtime_provenance`（`contracts.py:2118-2344`）。每个 runtime 域都必须有覆盖全字段的 `SemanticProvenanceV1`（`field_evidence: native_authoritative|native_derived|static_declared|not_applicable|unknown`），缺省即 unknown fail-closed（`contracts.py:986-1069`）。
- `EventV1`：`tick, event_type, owner, entity_id, card_id, position, visible, data, combat(CombatEventV1)?, runtime_provenance`（`contracts.py:2347-2392`）。
- `ActionMaskV1`：`kinds{wait:恒 true,...}, hand_slots[4], placement_masks, ability_sources, target_entities, reasons`（`contracts.py:2436-2469`）。
- `RasterV2`（`raster.v2`）：19 通道（含 5 个二值合法性通道 `legal_deployment_union, hand_slot_0..3_legal`），值有限非负，绑定 `source_action_mask_hash`（`contracts.py:70-94, 2472-2583`）。
- `PendingActionV1`：`action, requested_tick, expected_execution_tick(>=requested), generated_at_ms, requested_at_ms, inference_end_ms, server_apply_ms, status("queued")`（`contracts.py:2586-2615`）。
- 观察构建入口：`BattleEnvV1.observe(tier=FAIR, owner)`（`battle_env.py:4531`）；事件如 `action_requested / card_form_command_queued / ability_command_queued / action_rejected / action_canceled_terminal`（`battle_env.py:2408-2640`）。

---

## 6. 终局规则

`TerminalV1`（`terminal.v1`，`contracts.py:2618-2660`）：`ended, winner(0/1/null), result_by_owner([+1,-1] 零和), reason, terminal_tick`；ended ⟹ 必有 terminal_tick；winner ⟹ 其 result==+1。

判定顺序（`BattleEnvV1._terminal`，`battle_env.py:1396-1429`）：
1. `ended = raw.finalized|finalizedEnded|ended 或 任一 king 塔 inactive`。
2. winner 来源优先级：
   a. **native_final_result**：引擎 `winner`/`resultOwner` ∈ {0,1}；
   b. **king_tower_destroyed**：恰一方王塔毁 → 对方胜；
   c. **crown_advantage**：`_crowns`（王塔毁=3 冠，否则存活公主塔计数；优先取原生 `crownsRaw/crowns`，`battle_env.py:1370-1394`）多者胜；
   d. **draw_or_unexposed_tiebreak**：冠数相同 → 保留平局，绝不从隐藏状态发明赢家（引擎加时通常以打掉一座塔到 0 结束）。
3. 截断（truncation）：未终止且 `_canonical_tick(after) >= max_battle_ticks(7200)`（`battle_env.py:2700`）。决策合法域在 tick 6000 结束（`NATIVE_GAMEPLAY_END_TICK`，`match_factory.py:24-28`）。
4. 加时：由引擎时间线（`mode_timeline`，battle_timelines 分段圣水加速 28000/14000/9300ms，`battle_env.py:131-135`）体现；6000 tick 含 120 s 加时。
5. 王塔激活：从 remainingRuntime 的 `tower_activate`（hook 0xF23110, caller 0xF58820, result=true）事件观测（`cr_native_env.py:1779-1781`）——公主塔被毁后王塔激活是引擎内行为，宿主不模拟。
6. 奖励：`_rewards(before_towers, after_towers, terminal, dt)`（`battle_env.py:2701-2703`）。

---

## 7. 未决/不确定点清单（重实现必须核实）

1. **ordinary `observe` 完整 schema 未在 Python 侧定义**：Python 只消费部分字段（tick/players/objects/ended 等，§3.8）；`objects[]` 的逐字段清单、`accountId` 之外的玩家字段、`crownsRaw` 顶层形态只能从原生 probe 行为得到。重写模拟器需录制真实会话做黄金样本比对。
2. **引擎数值表不在此层**：卡牌数值、攻击时间线、命中判定、移动积分全在 libg 内。`CardSpecV1`（`card-spec.v1`）由 `card_specs.py` 编译，但 `unknown_fields`/`attributes` 表明显式 fail-closed；模拟器需要自建等价数值源。
3. **命令消费边界的精确语义**：`boundary = applied_tick - 1` 与 20-tick 年龄区间由 RE 得出（`cr_native_env.py:2899-2901`）；ct=86 之外的命令类型（ct=2 技能）由 probe 内部生成，宿主不可见其编码。
4. **`native hand card is invalid` 恢复语义**：手牌轮转期的"有界描述符哨兵"（`cr_native_env.py:3180-3188`）—— 模拟器需要复现该暂态或放宽之。
5. **hookOffset/callerOffset/vtable 常量**：全部 §3 表中的 0xFxxxxx 偏移是特定 libg build（`libg_build_id 90e6f351...`，`visibility_runtime.py:26-27`）的产物。模拟器若不伪造这些字段，所有引用它们的校验需要以"逻辑等价事件"替代；若要逐位兼容客户端校验，则必须原样输出。
6. **owner 相对可见性无生产者**：`publicByOwner/targetableByOwner` 永远 `unavailable`（`cr_native_env.py:1445-1455`）；公平观察的可见性过滤只能基于 invisibleCount 派生（`visibilityState`）， атак-targeting gate 只在 contextual 层（`visibility_runtime.py:345-354`）。模拟器需决定公平可见性的替代规则。
7. **capability/provenance 状态矩阵**：`_validate_rich_observation` 对约 40 个 provenance 键与 22 个 capability 的期望状态是硬编码的（`cr_native_env.py:1996-2098`）；模拟器若声称 `derived`/`authoritative` 必须语义一致，否则需整体降级为 `derived` 并调整客户端期望。
8. **`observe-atomic` 的原子性**依赖 probe 在同一管理器锁内编码两个视图（`cr_native_env.py:2773-2781`）；单线程模拟器天然满足，但需保证 tick/generation/stateEpoch/count 完全一致。
9. **`generation` / `stateEpoch` 语义**：只知道"reset/configure 递增、事件环以 epoch 划界"（`epochFirstSequence` 等）；具体递增规则未在宿主侧定义。
10. **圣水时间线**：`ELIXIR_SEGMENTS`（`battle_env.py:131-135`）来自 battle_timelines.csv，是精确生产调度，但"每 tick 恢复的定点数值取整方式"（elixirRaw 的更新规则）在引擎内 —— 模拟器需用 `ticks_for_raw` 反推并与真实 `elixirRaw` 对齐。
11. **`replay-schedule-*` 族**：probe 在 `execute_tick-1` 边界"读取实时手牌/形态"解析卡片（`cr_native_env.py:2988-2996`），其名称提示匹配（hints）算法只在 probe 内。
12. **住留模式细节**：`resident_batch_channel.py` / `resident_batch_vector.py` 未在本任务读取范围内；若重写引擎要支持住留 16 槽，其批量协议需另行抽取。
13. **native-render 模式**：`pause/resume/speed/advance-native` 的渲染器同步行为（`cr_native_env.py:2640-2826`）依赖 EGL 场景，纯模拟器只需实现 headless 路径即可覆盖评估需求，但协议层仍需回显兼容响应。
14. **快照（snapshot-create/restore）**：进程内无损快照（`cr_native_env.py:2835-2859`）；模拟器需定义可序列化状态以支持分支。
15. **`special/action/character/remaining` runtime 的全部枚举值**：character state 的 0..16 状态值含义被刻意匿名化（"deliberately retaining the engine's integer states"，`character_state_runtime.py:1-7`）；模拟器可以保留整数但失去了语义命名。
16. **`TowerState.tower_troop_id` 池**是"当前竞技池"（4 个 ID），随游戏版本变化（`contracts.py:60-67`）。

---

## 附录 A：关键常量速查

| 常量 | 值 | 锚点 |
|---|---|---|
| 协议版本 | `cr-native-control.v1` | `contracts.py:38` |
| 默认端口 | 26789 | `cr_native_env.py:34` |
| 响应上限 | 16 MiB | `cr_native_env.py:56` |
| configure 载荷上限 | 64 KiB | `cr_native_env.py:2675-2676` |
| inject 载荷上限 | 2 KiB | `cr_native_env.py:2869-2870` |
| MAX_TRANSITION_ACTIONS | 8 | `cr_native_env.py:35` |
| 对象上限 | 256（普通/富一致） | `cr_native_env.py:53-54` |
| 组件槽上限 | 64 | `cr_native_env.py:58` |
| 活动效果上限 | 64（名字 <=128B） | `cr_native_env.py:59-60` |
| 事件环容量 | combat 1024 / phase・visibility・remaining 4096 / special・action 1024 / character 2048 | `cr_native_env.py:64-67`；各 runtime 模块 |
| entityKey 形态 | `[owner, -2, nativeObjectId]` | `cr_native_env.py:55, 292-296` |
| Mirror 卡 ID | 28000006 | `cr_native_env.py:141` |
| 攻击序列进度上限/衰减 | 50 / 7000 ms | `cr_native_env.py:49-50` |
| 阶段进度→stage | <4→0, <9→1, <49→2, else 3 | `cr_native_env.py:1252` |
| 技能 attestation 宽限 | max(40, cast_ticks+20) ticks | `battle_env.py:148-149, 2476-2479` |
