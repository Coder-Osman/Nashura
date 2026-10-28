# Codex 回执：Linux catalog 资源轨迹分析

- **IMPLEMENTED**：新增 SHA 绑定的资源轨迹审计器及拒收测试；不发布原始 syscall 和资源名。
- **RUNTIME_OBSERVED_LIMITED**：已有真实 libg 运行中 247 次 APK open/read/close、
  131 个 update 文件打开与映射；122 个 update 文件与 APK 暂存同名，其中118个 SHA 不同。
- **UNIT_PASS**：Linux 专项 `964 passed, 1 skipped`，exit 0。
- **CHECK_PASS**：主工程211通过、清单2591项通过，gitleaks无发现；完整公开回归沿用父阶段结果。
- **NETWORK_FAILURE**：本轮 fetch GitHub 443 连接超时，未取得新的远端状态。
- **UNRESOLVED**：路径集合证据不证明单次请求因果，mmap 不证明字节消费；Android/Linux
  资源矩阵、Backup 等价和严格 catalog 门仍未闭合。
- **NOT_RUN**：ResourceManager、GameState、真实 Step、完整对局及 10×。

代码基线 `39a32fe46bede40004137e86a6d845f0d9979ff7`；交付提交 SHA 以包含本回执
的提交为准。详见[报告](PURE_LINUX_CATALOG_RESOURCE_TRACE_2026-10-28.md)与
[机器证据](pure-linux-catalog-resource-trace-evidence-20261028.json)。
