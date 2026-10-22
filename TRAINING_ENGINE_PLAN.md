# 面向训练的 CR 引擎重写 —— 前置知识 + 行动方案

> 日期：2026-09-08。本文是把一次长会话压缩成的一站式背景与路线，供"新写一个专门为训练用的引擎"这个项目直接开工用。
> 文末的锚点都来自本次会话实际读过的代码；凡标注"待核"的，开工第一步要回原文确认，别当作定论。

---

## 0. 这份文档在说什么

结论先行：
1. 现项目（FirstLight CR）的"引擎" = 真实的《皇室战争》客户端（Null's Royale 15.535.13，**只有 arm 库**），靠 probe hook 它来做训练/对局。
2. 本次会话确认：手头这台 x86 Linux 远端**跑不了 arm 客户端**（emulator 拒绝 arm64 镜像、无 ARM 转译），Windows+MuMu 或 arm64 宿主机才是仓库设计的引擎宿主。
3. 决定转向：**不依赖原版客户端，新写一个能驱动训练的高吞吐引擎**，顶替"引擎"这一层。

本文提供：(A) 让新引擎立项必需的前置知识；(B) 分层路线图；(C) 可立即开工的下一步。

---

## 1. 系统现状与硬约束

### 1.1 为什么"原版引擎"被卡住（本会话实证）
- 引擎 = 客户端内部的对战模拟器（`libg.so`，Supercell Titan 系）。支持指纹见 `native_runner/supported_engine.json`：
  - 包 `nullsroyale.rel.free`，15.535.13 / arm64-v8a；APK sha256（原版）`685c115e…`
  - 运行期内容版本 **15.535.86**，`runtime_fingerprint_sha256 = e0cb2fb9…`（首次联网下载、留在设备里）
- 本机模拟器 emulator 37.1.11 实测：`Avd's CPU Architecture 'arm64' is not supported by the QEMU2 emulator on x86_64 host.` 且系统无任何 ARM 转译层（无 libndk_translation/houdini），x86_64 镜像是纯 x86 stock。→ 原生对局在**这台远端不可行**。
- 仓库文档设计（`docs/training.md` §6）：Linux 只做训练/引擎集群的**消费方**，离线 APK + 已备好内容的基础 AVD 要在 Windows+MuMu（带 ARM 转译）上先做好再拷过来。

### 1.2 本会话已核实的资产/状态
| 项 | 位置/状态 |
|---|---|
| 仓库完整代码 | 本地 `firstlight_CR/` 与远端 `~/firstlight_CR/` 已逐字节一致（rsync + MD5 核对） |
| Python 环境 | 远端 `.venv` torch 2.5.1+cu121、3×V100、`tools/check_install.py` 通过（5 模型全兼容） |
| 模型 | `checkpoints/`：General、IL、active IL、`2_6hog_expert/hog26-specialist1/2.pt`（速猪 2.6）等 5 个 |
| 输入 APK | 用户自备重打包包 `01b375e8…`（**非**原版 `685c115e…`），但 `lib/arm64-v8a/libg.so` 与 159 个 `csv_logic` 内容与原版**逐字节一致**；已放宽 `offline_build.py` 校验以接受它 |
| probe | 远端用 NDK linux 交叉编译成功（`probe/out/libcrprobe.so` 7.5M） |
| 离线 APK 构建 | 写了 Linux bash 驱动 `remote_build_engine.sh` 复刻 PowerShell 流程，**尚未端到端跑通**（decode/smali 那步没验） |
| 内容更新 15.535.86 | 不存在任何机器上，需原版客户端联网首跑下载 → 无 arm 宿主则拿不到 |
| 模型/环境兼容 | competitive data（`data/competitive/*.json.gz` + manifest 哈希）已就位 |

### 1.3 IP / 许可注意
- 项目 `NOTICE`/README：不分发 Supercell 二进制与资源，用户自行提供 APK。
- 新引擎请**独立实现逻辑**，只把功能性数值当输入（卡的 stats/机制数据，即自己派生/持有的 competitive 表）；不要搬运游戏二进制或受版权保护的素材。

---

## 2. 引擎 / 客户端 / 训练 的关系（压缩版）

```
客户端(APK,内含引擎 libg.so) ──probe(桥)──> Python 环境(NativeClashEnv/BattleEnvV1)
                                              └──> V4 策略 / IL / PPO 训练器
静态数据: 客户端 assets/csv_logic ──resource_compiler──> competitive 表 ──> 模型特征/解码
```
- **引擎 = 会打游戏的客户端**，是唯一"真打"的东西（寻敌/移动/弹道/塔/法术/圣水/加时全在里面）。
- probe 只是桥（注入 .so + 控制口），"离线引擎 APK"= 原客户端 + probe + 多进程入口，不是新引擎。
- Python 环境把引擎状态→观测、把策略输出→动作→引擎，见 `docs/architecture.md` §"One decision"：**模型每 5 个原生 tick 决策一次**。
- 训练（IL 回放重建、PPO 自博弈、评测）全部在引擎里跑 → **引擎是 Ground Truth 也是吞吐瓶颈**。RL 吞吐 ≈ 并发引擎槽位数 × 单局速度；瓶颈在引擎不在网络/GPU 推理。
- 因此：重写一个"忠于契约、又更快更可并行"的引擎，能同时解决"无 ARM 宿主"和"吞吐瓶颈"两个问题——但要付出保真（sim-to-real）成本。

---

## 3. 新引擎必须满足的契约面（核心前置知识）

新引擎能否"无缝顶替"，取决于它能否复刻 `NativeClashEnv` 暴露的**接口 + 语义**。分层看：

### 3.1 传输边界（本会话确认）
- `NativeClashEnv`（`native_runner/cr_native_env.py:2479`，host `127.0.0.1`，port 默认 **26789**）：基于 `socket` 的 TCP **JSON** 会话；响应里带运行方 attestation + 多张 schema 化的遥测/观测。相关 schema 常量都在这文件顶部：
  `native-rich-telemetry.v3`、`native-combat-events.v1`、`native-phase-runtime.v1`、`native-visibility-runtime.v1`、`native-remaining-runtime.v1`、`native-observation-capture.v1` …
- 每个 schema 都有对应 Python 解析器，即"语法说明书"：
  `action_movement_runtime.py`、`character_state_runtime.py`、`phase_runtime.py`、`special_movement_runtime.py`、`battle_env.py`(构建观测) 等。
- **这条边界是新引擎要 speak 的"协议"**。方案 A：新引擎直接在同一 TCP/JSON 上应答 → `battle_env`/`evaluate`/训练器**一行不改**。方案 B：在 `BattleEnvV1` 后面加第二个 backend。推荐先评估 A，A 不通再 B。

### 3.2 必须精确复刻的"引擎语义"（难在这，不是协议）
probe 靠 hook 真引擎函数地址（如 `0xF23110` attack_start）抓"事件"；也就是说**这些 schema 编码的是真引擎的内部事件流**。要让已训好的 V4 策略正常工作，观测必须在同一 tick 刻度上携带等价信息。至少包括：
- 决策节奏：每 5 native tick 一决策；`FIRST_PLAYABLE_TICK=90`；出牌等待/部署时序（native deployment timing）语义。
- 观测要素：实体/关系/空间特征、FAIR 可见性契约、最多 256 对象、塔/国王塔状态、圣水、攻击序列、剩余时间等（见 `battle_env.py` + `contracts.py` 的 `ObservationV1`）。
- 动作：`ActionV1`（部署到竞技场格、卡牌、技能），合法性与"拒绝"事件语义。
- 终局：胜负（含平局/超时/截断）、加时、国王塔激活。
- 范围锁定：模型冻结在 **normal-mode、level 11、122 卡**（见 competitive manifest），先只做这个子集。
- 事件可选性：headless 采集用 `record_trace=False/True`、`TrainingReplayV1`（回放轨迹）——评测与"人看"靠它。
- **待核**：每张 schema 的 JSON 字段级定义、动作如何编码成"命令"、拒绝/重试语义，要读上面列的文件逐条抠出，写成交互规范（见路线 P0）。

### 3.3 可整层复用、绝不动的东西
- **模型侧**：`training/v4/{model,tensorizer,decoding,policy_session,factory}.py`、5 个 checkpoint、competitive 表。它们与"引擎"只通过观测/动作打交道。
- **数据侧**：`data/competitive/*`（卡数值/机制/词表，即新引擎的"规则库"来源之一）；HF `IL_Replay` 数据集（训练与回放素材）。
- **训练器**：若走方案 A（协议兼容），IL/PPO/evaluate 全部复用。
- **测试**：仓库大量 `test_*.py`（尤其 `test_battle_env*`、`test_training_v4_*`）可当一致性套件。先跑一遍"不依赖引擎也能跑"的测试当基线。

---

## 4. 路线图（里程碑）

### P0 — 抽"契约圣经"（先做，产出 wire-spec.md）
把 3.2 的每个未知抠成规范：
1. 通读 `cr_native_env.py` 全部（协议、命令、重置/推进、attestation、resident）。
2. 通读各 `*_runtime.py` 解析器 + `battle_env.py` 的 reset/step/观测构建 + `contracts.py` 的 `ActionV1/ObservationV1/MatchConfig/terminal` + `probe/*.h`（`event_ring_json.h`、`*_layout.h`）看"引擎视角"的状态形状。
3. 产出 `wire-spec.md`：请求/响应消息、schema 字段、tick 语义、合法动作规则、终局规则、最小可跑回放格式。

### P1 — 最小闭环引擎（sim 核心）
- 语言/形态：建议先纯 Python + 确定性 tick（复用 `resource_compiler` 的编译表当规则输入，`card_specs/card_logic/effects/projectiles`）。为吞吐可后补 C/Rust 或 PyPy，但**先别重构过猛**。
- 范围：normal-mode level 11；先做速猪 2.6 那几张卡的子集（野猪/冰人/火枪/骷髅/火球/滚木/加农/冰晶）把闭环跑通，再扩到 122。
- 产出：能 reset/step、能发合法动作、能出终局的确定性对局核心 + 单元测试。

### P2 — 环境适配（两条路，先 A）
- **A** 实现一个 speak `NativeClashEnv` TCP/JSON 协议的"sim runner"，宿主回环进程常驻，让 `evaluate.py`/训练器**不改一行**就连上（回报最大、验证最快）。
- **B**（A 不可行时）在 `BattleEnvV1` 后加第二 backend，改训练入口少量接线。
- 验收：`evaluate --checkpoint-a hog26 --checkpoint-b IL --games 2` 在 sim 上能跑完并产出 `matches.jsonl/summary.json`。

### P3 — 保真与一致性验收（绕不开一次真引擎基准）
- 需要一个"真引擎 ground truth"来验收 sim，哪怕只临时用一台 MuMu/arm：
  - 用同 seed 的真引擎跑同一对局/回放，导出轨迹（`TrainingReplayV1`/HP 曲线/终局），与 sim 对齐比较。
  - 或用 HF 回放数据集经真引擎重建的轨迹当金标准。
- 定义"够用"指标：终局一致率、动作合法率、公开事件/HP 轨迹偏差；sim-to-real gap 是否破坏已训策略（速猪仍打得像样）。
- **没有这份基准之前，任何"看起来能跑"都只能算演示，不能算验证**。

### P4 — 吞吐与训练接入
- 多进程常驻、可并行多局、速度倍率；与 trainer 异步流水；测 steps/sec 对比"MuMu 真引擎"作为收益线。
- 若走协议复用，`resident`/batch 通道语义也要在 P0 写进规范。

---

## 5. 下一步（立即开工，按序）

1. **开独立工作区**：建议在 `firstlight_CR` 仓库内以"第二引擎后端"的方式起步（模型/数据/训练原地复用，改动最小、每天可见进度）；想干净隔离再抽成新 repo。
2. **跑基线测试**：`firstlight_CR` 里跑一遍不依赖引擎的 pytest，确认现状绿、识别可复用的 conformance 测试。
3. **P0 契约抽取**（产出 `wire-spec.md`），文件清单见 §3 与 §7。
4. **定 sim 数据源**：确认用现有 competitive 表（`data/competitive/*.json.gz` + `resource_compiler/*.py`）能支撑 P1 子集；补一张"卡→机制→数值"查表可用的清单。
5. **P1 最小闭环**：子集卡、确定性 tick、无 UI，先让"脚本 vs 脚本"打完一局并导出轨迹。
6. **P2 接模型**：把 `hog26-specialist1.pt` 接上去，验证动作解码合法、能正常推进对局（速猪该下的牌要下得出来）。
7. **准备验收基准**：确定一台可临时使用的真引擎宿主（MuMu/arm），或确认用 HF 数据集重建轨迹做金标准——P3 前必须定。

---

## 6. 关键文件索引（锚点；开工时回读）

| 主题 | 文件（含本会话已知锚点） |
|---|---|
| 一次决策/分层总览 | `docs/architecture.md`（§One decision，决策 5 tick） |
| 引擎传输/协议宿主 API | `native_runner/cr_native_env.py:2479 NativeClashEnv`、`3205 ResidentNativeClashEnv`；顶部 schema 常量与 tick 常量 |
| schema 解析器（协议语法） | `native_runner/{action_movement_runtime,character_state_runtime,phase_runtime,special_movement_runtime,visibility_runtime}.py` |
| 环境（obs/action/终局/渲染模式） | `native_runner/battle_env.py`（`BattleEnvV1` 需 native 后端）、`native_runner/contracts.py`（`ActionV1/ObservationV1/MatchConfig`，render_mode 在 :2853） |
| probe 原生状态形状 | `native_runner/probe/{cr_replay_probe.cpp,event_ring_json.h, *_layout.h, *_telemetry.inc}` |
| 静态规则/编译表（sim 的数据源） | `native_runner/resource_compiler/*.py`；产物 `native_runner/data/competitive/*.json.gz` + `manifest.json`（逐文件哈希）；`mechanics.py` 头注释："native 是唯一事实源，本模块只投影" |
| 评测（headless 模型 vs 模型） | `native_runner/training/v4/evaluate.py`（连 `127.0.0.1:26789`，产出 `matches.jsonl/summary.json`） |
| 模型侧（别动） | `native_runner/training/v4/{model,tensorizer,decoding,policy_session,factory,offline_agent}.py` |
| 训练架构/并行/容量 | `docs/training.md`；`training/v4/{cluster_self_play,async_cluster_self_play,resident_batch_channel,resident_batch_vector}.py`（1–24 引擎/VM、1–8 槽/引擎等） |
| 版本指纹/构建/安装 | `native_runner/supported_engine.json`、`offline_build.py`（已放宽输入校验）、`offline_install.py`、`attestation.py` |
| 模型 | `checkpoints/{General,IL,active IL,2_6hog_expert/hog26-specialist1|2.pt}` |
| 配置键 | `.env.example`（`CR_*`） |

---

## 7. 未决问题 / 主要风险

1. **保真门槛**：sim 必须忠实到何种程度才能保住已训策略？（建议以 P3 指标定"可接受"，而不是追求逐字节一致——那是无底洞。）
2. **122 卡机制工程量**：寻敌/弹道/法术/AOE/群体/状态/塔 troop/国王激活/加时/技能/进化形态……先做 normal-mode 子集，用速猪当"试金石"。
3. **观测与事件 1:1 问题**：FAIR 可见性 + 攻击序列 + phase 事件是模型输入，sim 事件流要和 schema 语义对齐（P0 抠清）。
4. **动作合法性/部署时序**：`FIRST_PLAYABLE_TICK`、native deployment timing、拒绝重试，直接影响 IL 重建与策略行为。
5. **验收基准缺失**：至今没有任何真引擎 ground truth；P3 前必须搞到一份（临时 MuMu/arm 或 HF 重建轨迹）。
6. **工程量诚实评估**：这不是"小重构"，是"再造一个引擎"；用 P1→P2 的可跑 demo 每步校验，别一口气铺 122 卡。
7. **吞吐**：纯 Python 慢；先保证语义正确，再用多进程/加速/（必要时）native 化单点替换。
