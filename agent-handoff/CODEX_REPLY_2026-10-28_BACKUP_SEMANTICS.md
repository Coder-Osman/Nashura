# Codex 回执：E2.1-N Backup callback 语义调查

- **IMPLEMENTED**：新增服务端 DEX 有界调用摘要器与 3 项测试；摘要仅含 allowlist 方法调用边、控制流类别和指令计数，不发布原始指令。
- **UNIT_PASS**：`python3 -m pytest -q -p no:cacheprovider tools/pure_linux/test_backup_server_audit.py tools/pure_linux/test_backup_trace_audit.py tools/pure_linux/test_backup_method_audit.py`，exit 0，49 passed。
- **RUNTIME_OBSERVED（历史 trace 重分析）**：835,263 records；23 个请求、2 个线程、23 次正常返回；scope 内未知记录0、栈平衡。不是本轮新 Android 运行。
- **STATIC_FOUND**：匹配 Android 30 服务端11方法；发现权限/用户检查、Handler.post、请求登记、journal 写入和 `KeyValueBackupJob.schedule` 调用边。
- **STATIC_FOUND**：客户端 DEX 中 transaction code 2、flags 0、独立 request/reply Parcel；同步事务。无动态参数捕获。
- **UNRESOLVED**：真实服务端交付、权限/ready分支结果、journal/job实际副作用及完整配置 callback 返回契约。
- **BLOCKED**：严格 `CATALOG_READY_PASS=false`；ResourceManager BLOCKED；GameState/Step NOT_RUN。

## 实际输入与输出

- 私有服务端 DEX SHA：`66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a`；Android 30 平台归档SHA：`c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
- 服务端分析命令：`python3 tools/pure_linux/backup_server_audit.py --dex <PRIVATE_SERVICES_CLASSES_DEX> --dex-sha256 66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a --dexdump <MATCHED_DEXDUMP> --dexdump-sha256 9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692 --output <NEW_PRIVATE_AUDIT_DIR>`；exit0，报告SHA `4e0c5f8798f80a82b89ae582269b36d331cbe2a3917d6e22ff3dc69c9f09564b`。
- trace重分析命令：`python3 tools/pure_linux/backup_trace_audit.py --trace <EXISTING_PRIVATE_ART_TRACE> --trace-sha256 01bc15eba14b2ba50b0b5cb66763f1be1ecf4e35265d832f672607264dbf52d2 --output <NEW_PRIVATE_TRACE_REPORT>`；exit0；脱敏报告SHA `82cd3fdb3a25f2d0874e1c4fba198e61d53077160ad431f3a59f3dfd0f85aacb`。
- Binder边界失败复核：v121 两个独立 Android linker/Bionic 子进程均在 `AServiceManager_checkService("backup")` 前报告 `/dev/binder` 打不开并 `SIGABRT`；服务发现返回和服务端 callback 均未观测，不能解释为服务不存在。
- 脱敏 JSON：[`pure-linux-backup-semantics-evidence-20261028.json`](pure-linux-backup-semantics-evidence-20261028.json)。原始 DEX、trace 与日志未发布。

## 下一步

先获取可观测的真实服务端 callback 接收/返回及权限、用户 ready 结果，或完成受控服务端对象图；保持失败子进程记录和严格 gate。不得以 no-op callback、合成成功或静态调用图放行 catalog。没有启动 Emulator、训练、GameState 或吞吐实验。
