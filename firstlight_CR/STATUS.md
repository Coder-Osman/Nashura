# Sim 引擎项目状态交接（2026-09-08，由远端 opencode 接手）

## 一句话目标
重写一个纯 Python 的确定性对局引擎（sim），顶替"真 CR 客户端引擎"，让 IL/PPO 训练跑在它上面。
训练环境契约（TCP/JSON 协议、tick 语义、观测/动作）见 **`wire-spec.md`**（P0 产物，378 行，全部带 file:line 锚点）。

## 里程碑状态
| 里程碑 | 状态 | 说明 |
|---|---|---|
| 基线测试 | ✅ | 远端 pytest 1187 用例：1175 过 / 2 失败（与 sim 无关：offline_build APK 放宽校验的副作用 + probe C++ fixture 快照） |
| P0 契约抽取 | ✅ | `wire-spec.md`（本目录） |
| P1a 规则数据 | ✅ | 8 卡数值已从 `model_catalogs.json.gz`（等级 11 投影）+ 解包 csv_logic（`/tmp/csv_logic`，SC 压缩格式，需 sc_compression 解）核对；塔 HP/伤害按 globals 8%/级推到等级 11 |
| P1b sim 核心 | ✅ | `native_runner/sim_core/`：`ruleset.py`（数值+来源注释）、`state.py`、`engine.py`（tick 循环/战斗/终局）、`env.py`（SimClashEnv）。已修：手牌 8 卡循环补牌、法术 0 部署延迟 |
| P1c 测试 | ✅ | `test_sim_core.py` **18 passed**（2026-09-08 远端 opencode 修复；两真 bug：queue 循环耗尽、spell 误用 1s 部署延迟；一测试自伤问题已重写） |
| P2 sim runner | ⬜ | **路线待定：方案 A（wire 协议复刻，evaluate.py 零改动）vs 方案 B（BattleEnvV1 后加第二 backend）**，见 STATUS 文末决策点 |
| P3 保真验收 | ⬜ | 需要真引擎基准（MuMu/arm 或 HF 回放金标准），目前没有 |

## 当前失败测试与已知线索（接手者从这里开始）
```
.venv/bin/python -m pytest native_runner/tests/test_sim_core.py -q
```
失败 6 个（其余 12 过）：
1. `test_king_activates_when_princess_dies` —— 6 连 Hog 打不穿公主塔；怀疑部署节奏/伤害期望与塔回复循环不匹配。可放宽为连续进攻直到击杀（白盒注入手牌+满圣水已做，见测试）。
2. `test_placement_rules` —— 最后一段火球任意位置断言失败；测试本身有一行双否定写法残留（`assert not ... is False`），需要重写清晰。
3. `test_scripted_match_terminates_deterministically` / `test_match_ends_within_gameplay_bounds` / `test_reset_restores_fresh_state` —— 脚本对局相关，可能是 env/脚本死循环或终局没在 6000 tick 内触发。
调试建议：先单独跑 1、2；`_mirror_script` 加 tick 上限保护（如 7000 tick 强制退出）避免死循环拖死测试。
规则：**修代码或修测试均可，但不许放宽数值锚点**（ruleset.py 里的等级 11 数值是 competitive 表事实，见各常量注释来源）。

## 环境速查
- 仓库：`~/firstlight_CR`（git 已初始化；未提交的改动：sim_core/、test_sim_core.py、wire-spec.md、TRAINING_ENGINE_PLAN.md、STATUS.md、offline_build.py 历史放宽）
- Python：`.venv`（torch 2.5.1+cu121），8×V100 空闲；pytest 已装
- 卡数据：`native_runner/data/competitive/*.json.gz`（等级 11 数值在 model_catalogs.json.gz 的 card_feature_rows）
- 解包客户端资产：`/tmp/csv_logic/assets/csv_logic/`（characters/*.toml 单位机制、projectiles.toml、globals.csv 塔缩放）
- 速猪 2.6 子集 8 卡 id：HogRider 26000021 / IceGolem 26000038 / Musketeer 26000014 / Skeletons 26000010 / Fireball 28000000 / Log 28000011 / Cannon 27000000 / IceSpirit 26000030

## 下一步（P1 绿后）
1. P1 完成标准：脚本 vs 脚本打完一局并导出轨迹 —— `test_scripted_match_*` 已覆盖"确定性打完一局"，轨迹导出格式留待 P2 按 `TrainingReplayV1` 补。
2. **P2 路线决策（未定）**：
   - 方案 A：sim runner 在 26789 说原生行协议 → `evaluate.py`/训练器一行不改。成本：客户端 `NativeClashEnv` 对响应校验极严（`_validate_rich_observation` 硬编码 22 项 capability/40 项 provenance 期望状态、ring 信封不变量、hookOffset/vtable 常量、attestation digest），sim 需要伪造/对齐这些（wire-spec §7 未决点 5-7）。
   - 方案 B：在 `BattleEnvV1` 后面加第二个 backend（sim 直连，跳过 NativeClashEnv 协议层），训练入口少量接线改动。成本低、不造假，但"零改动复用"打折扣。
   - 建议：先让 opencode 出一份 A/B 落地评估（读 battle_env 与 cr_native_env 的校验面，估算 A 需要实现/伪造的最小面），再定。
3. 重要风险：`observe` ordinary schema、attestation/hook 常量、塔基值（KingTower 2400 vs KingTower3 3000 有歧义，现用 2400）都需要 P3 真引擎基准校准。
