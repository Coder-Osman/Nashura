# .3 内容定点补丁记录（2026-09-08）

## 背景
设备/APK 内容为 15.535.3（fingerprint f6774032…），competitive 表由 .86 增量（81 csv 覆盖）编译。
全量差分（152 卡）显示 .3 与表仅 19 处字段差；速猪 2.6 子集唯一差 = **Ice Spirit 基础 HP 84→90**（等级 11：215→230）。
决策：定点补丁（不改编译器、不全量切 .3），把 Ice Spirit 数值对齐设备 .3，使真引擎（.3 设备内容）与表一致。

## 改动清单
| 文件 | 改动 |
|---|---|
| data/competitive/card_logic.json.gz | IceSpirits base hp 84→90、scaling candidates、field_level_projections（含 source_candidates）→ 90/230；card_specs_hash、ability_specs_hash、card_catalog_content_hash 重算 |
| data/competitive/card_specs.json.gz | 26000030 hitpoints 84.0→90.0 |
| data/competitive/model_catalogs.json.gz | level_hitpoints 215→230；card_catalog static_features[30][1]（hp/5000）0.043→0.046；input_catalog_ids 重算 |
| data/competitive/effects.json.gz / projectiles.json.gz | card_logic_catalog_id → 运行时 static logic catalog_id f35917ee… |
| data/competitive/card_support.json | compatibility.static_catalog_id → f35917ee… |
| data/competitive/form_evidence.json | evidence.card_specs_hash → 3da96348… |
| data/competitive/manifest.json | 上述文件 files[] 哈希 + 常量重生成 |
| native_runner/competitive_data.py | MANIFEST_SHA256 → 994f8dee… |
| native_runner/semantic_subset.py | NORMAL_MODE_POLICY_CARD_SPECS_HASH → 3da96348…；STATIC_CATALOG_ID → f35917ee… |
| native_runner/normal_form_evidence.py | NORMAL_MODE_POLICY_FORM_EVIDENCE_SHA256 → c413ba5f…（evidence_id 公式） |
| tests/test_competitive_data.py | 冻结身份字面量 → 新值 |

## 哈希链公式（供日后全量重编译参考）
- files[].sha256 = sha256(canonical sorted indent-2 json +\n)
- files[].content_hash = contracts.content_hash(obj)
- card_logic.card_specs_hash = content_hash(specs)；ability_specs_hash = content_hash(abilities)
- card_catalog_content_hash = content_hash(整个 card_specs 对象)
- 运行时 static logic catalog_id = build_static_card_logic_catalog(card_catalog).catalog_id（= content_hash(card_logic json)）
- effects/projectiles.card_logic_catalog_id = 上述运行时 id
- form_evidence evidence_id = content_hash(对象含 version 字段)
- MANIFEST_SHA256 = sha256(manifest.json 字节)

## 残留风险
- 其余 18 处 .3↔表差仍在（非速猪卡；日后全量训练需按 P2-B 全量重编译路线处理）。
- RamRider 差疑似映射假象，未核。
- 本补丁后 ruleset_id/catalog 身份改变 → 既有 checkpoint（.86 训练）输入分布仅差 Ice Spirit hp 一行；需 evaluate 复评确认影响。
- 备份：git HEAD 可恢复全部原始数据。
