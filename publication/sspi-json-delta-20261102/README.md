# SSPI JSON 增量复现

两个脚本仅重配置用户选定的专用测试 worker。先确认该端口没有 actor、没有未归档对局，且属于现有 macOS ARM64 Android Emulator；不执行包级 debug-app、整体部署脚本、AVD 重启或 adb 转发变更。保留既有输出，用全新的输出目录运行。

依赖当前仓库 Python 环境和相同源码编译的 probe。通过命令环境传入 PYTHONPATH（指向仓库 firstlight_CR）和 CR_PROBE（指向实际已部署、basename 为 libcrprobe.so 的本机文件）；完整校验必须开启。probe、APK、权重、原始捕获不随仓库发布。

运行示例（在仓库根目录，PORT/PYTHON/PROBE_PATH 为检查过的本机配置）：

    PYTHONPATH=firstlight_CR CR_PROBE=PROBE_PATH PYTHON publication/sspi-json-delta-20261102/search_compare.py --port PORT --output OUTPUT_DIR --transport persistent
    PYTHONPATH=firstlight_CR CR_PROBE=PROBE_PATH PYTHON publication/sspi-json-delta-20261102/search_compare.py --port PORT --output OUTPUT_DIR --transport persistent-delta

按上述顺序串行运行，逐项比较两个 JSON 中的 candidate_distribution、selected_action、fair_sha256、model_sha256、valid_simulations、native_ticks 和快照结果；128次有效模拟、固定seed 20261103。它先记录实际开局FAIR，再在同一测试进程独立重建belief；physical_actual_root_preservation_tested=false，不能将此当作双worker的真实根物理状态保留验收。

捕获边界审计同样串行运行，full与delta使用相同run/game/stream身份：

    PYTHONPATH=firstlight_CR CR_PROBE=PROBE_PATH PYTHON publication/sspi-json-delta-20261102/capture_boundary.py --port PORT --output FULL_OUTPUT_DIR --transport event-json --run-id sspi-host-history-20261102
    PYTHONPATH=firstlight_CR CR_PROBE=PROBE_PATH PYTHON publication/sspi-json-delta-20261102/capture_boundary.py --port PORT --output DELTA_OUTPUT_DIR --transport event-json-delta --run-id sspi-host-history-20261102

比较 boundary-audit.json、combat-audit.json，以及 partial.pt 中的动作。记录按跨运行generation/epoch、各环sequence/causeSequence及进程级deploymentSequence基点归一化，保留相对关系和战斗字段。当前模型和轨迹会因原生出牌拒绝产生 quarantine，脚本保留非零退出码和边界审计；不把它当作训练成功。partial.pt仅对本机可信来源用torch.load读取。额外摘要计算的耗时不作为性能结论。

正式计时、435边界等价、128根搜索和部署事故范围见 docs/SSPI_JSON_DELTA_EVIDENCE_2026-11-02.json 与 docs/SSPI_JSON_EVENT_DELTA_2026-11-02.md。该阶段未产出新有效训练episode或模型强度结论。

函数级搜索剖析使用profile_search.py，传入相同的--port、全新--output和--transport persistent或persistent-delta。它只给现有函数加计时，根结束后对已捕获FAIR输入做本地摘要分析，不增加native请求；自动比较已发布基线的候选、动作、身份、tick和RPC计数。完整计时与子项有嵌套，按脚本说明区分inclusive/exclusive，勿累加重复时间。两种模式仍须串行在独立空闲worker上运行。
