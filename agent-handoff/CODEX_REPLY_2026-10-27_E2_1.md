# E2.1 执行回执

**结论：G-E2.1 BLOCKED，真实验证器 exit 2；E2.2 未启动。**
本次完成排除键账本、PN 原桥冷启动/状态矩阵、有界字符串生命周期和真实附件拒收测试，
尚未闭合 PN 持久化与其余 required 键，未新增 replacement 键。

## 分支与提交

- 分支：`codex/headless-engine-plan-20261025`；基线 `7c601300eb425b82981f8c78f0e4b2accb692490`。
- main：`0012201830cfbfcbfeb3d9dccd91a855028da869`，未修改。
- 本次提交 SHA：使用包含本回执的阶段提交，在 Git 历史及最终消息中核验；不预填虚构 SHA。

## 实际结果

- 46 个唯一排除键、99 次历史自然 fallback，全部 required，未移除。
- 九次成功独立进程各 3602 快照/28 动作/tick3601 终局，全字节一致；restore/reset 及
  17 个非轮询控制响应一致。未单独认证配置对象销毁或 OS teardown。
- 32 键 shadow/replacement/strict 仍为 32 读/4 写/targeted fallback0；总 fallback
  116/116/115 包含新增探针，未隐藏原桥依赖。
- PN 空、malformed、缺键后的三个自然冷启动转换及保存值恢复通过；PN 仍原 JNI。
- 原桥正常 probe 线程 ExceptionCheck 前后 false；异常触发与清理未覆盖。
- 七个自有 EngineString16 构造/析构/free 通过；配置 store 和 Java 引用生命周期未知。
- 初始化入口顺序/caller/线程已记录，布局、所有权、失败清理未知；两处 hook 未覆盖。
- 正常资源平台 AssetManager0、Provider failures0/live0，输入身份保持。
- 真实 strict 未知键负向按预期退出1；失败现场及调试失败索引保留。

## 命令与退出码

完整参数见 [报告](E2_1_CONFIGURATION_SEMANTICS_2026-10-27.md)。

| 命令/证据 | 结果 | exit |
|---|---|---:|
| E2.1 三组 pytest，使用真实附件 | 33 passed，9.13s | 0 |
| E2/账本五组回归，使用旧真实附件 | 37 passed，8.67s | 0 |
| 四份 JS 的 node --check | 通过 | 0 |
| run_main_engine_checks.py | 211 passed，32.36s | 0 |
| run_public_checks.py | 主组794 passed/27 skipped，八专项通过 | 0 |
| verify_e2_1_gate.py，gate-attempt-5 | bounded passed；strict BLOCKED | 2 |
| e2_1_public_summary.py | 三份脱敏 JSON 导出 | 0 |
| check_public_snapshot.py | 2433 项文件身份通过 | 0 |
| gitleaks dir . --config .gitleaks.toml --redact | 精确键名误报登记后无告警 | 0 |
| git diff --check | 通过 | 0 |

私有附件缺失时真实测试显式 skip；不算原生等价证据。严格门固定 fail closed，
认证范围是当前限定证据；不能以修改汇总 JSON 得到完整语义 PASS。

## 交付与下一步

账本、语义 JSON/Markdown、比较清单、门禁、失败索引、工具与独立测试均随阶段提交发布。
所有配置值只保留字节长度/SHA；不上传原始审计、私有配置或第三方文件。
本轮独占实验资源已回收，旧 adb/冻结任务保持；没有正式训练或10×结论。

第一阻塞是 PN 原桥的持久化提交/并发/异常/释放契约。下一实验先定位实际 Java 方法、
commit/apply 与跨进程可见性，再受控两线程差分，之后才设计 native store 扩展。
no-VM 冷启动、首 tick 和完整生命周期在本轮均未尝试，不沿用旧 preinit 成绩冒作通过。
