# Codex 回执：非空偏好计数修正与真实 libg 重跑

- **IMPLEMENTED**：非空 XML 使用绑定材料逐槽计算加密键，缺少材料或加密库则保持未知；修正此前明文键计数错误。
- **UNIT_PASS**：`/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux`，exit 0，957 passed、1 skipped。
- **CHECK_PASS**：主工程211通过；隔离副本公开回归1104通过/42跳过；清单2586项通过；gitleaks复扫无发现。
- **RUNTIME_OBSERVED_LIMITED**：已有编码键的两槽真实输入中，ARM64 Linux 的原版 libg 候选 native exit 0，实际与预期均 `[1,1]`，两次 commit、一次候选入队；wrapper exit 1。
- **RUNTIME_OBSERVED_LIMITED**：自然 loader ready，162 张表、24,353 行的布局；不包含逐 cell 对照。
- **BLOCKED**：Android Backup 语义等价、完整 catalog 资源语义及严格 `CATALOG_READY_PASS` 未闭合。
- **NOT_RUN**：ResourceManager、GameState、真实 Step、完整对局、Android 逐 tick 对照及 10×。

本地代码基线 `b6f92a23f8ca968abf06e26ddcb9a1d0969abd1e`；本回执交付 SHA 以
包含它的提交为准。命令、身份、退出码和范围见[报告](PURE_LINUX_NONEMPTY_PREFERENCE_RECHECK_2026-10-28.md)
及[机器证据](pure-linux-nonempty-preference-recheck-evidence-20261028.json)。
