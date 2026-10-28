# Codex 回执：受控 loading 配置事务（2026-10-28）

## 状态

本轮完成一次用户批准的真实 Android 配置写入并恢复原值。状态为
`RUNTIME_OBSERVED / LIMITED_PASS`，配置平台持久化契约仍为 `UNRESOLVED`，
`CATALOG_READY_PASS` 仍为 `BLOCKED`。

## 结果摘要

- `LOADING_SCREEN_STATE_LABEL`：空值 → `datatables-init` → 空值。
- 写入后读回和恢复后读回均符合预期。
- 两次写入均触发客户端 Backup 请求及 system_server 的异步处理和 job 调度。
- pending 队列维持 30，且目标包在实验前已存在，因此没有把数量变化误当作新 journal 证据。
- commit、磁盘写入、双槽文件和跨进程重启恢复仍未闭合；对应未观测事件保持未知。

## 实际命令与证据

```sh
node --check tools/pure_linux/backup_runtime_observer.js
# exit 0
python3 -m py_compile tools/pure_linux/run_loading_store_once.py
# exit 0
/tmp/nashura-frida-16.7.19-venv/bin/python tools/pure_linux/run_loading_store_once.py \
  --observer tools/pure_linux/backup_runtime_observer.js \
  --output /private/tmp/nashura-loading-store-transaction-20261028-102313/frida-events.jsonl \
  --system-server-pid 522 --game-pid 6901 \
  --adb "$HOME/Library/Android/sdk/platform-tools/adb" \
  --serial emulator-5580 --key LOADING_SCREEN_STATE_LABEL \
  --value datatables-init --restore-value ""
# exit 0
python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0；531 passed, 1 skipped；5.15s
```

原始 JSONL 保留在本机私有路径；脱敏字段和 SHA 见
`pure-linux-loading-store-transaction-evidence-20261028.json`。

## 门状态

| 项目 | 状态 |
|---|---|
| 受控写入/恢复 | `RUNTIME_OBSERVED` |
| Backup 请求到服务端 | `RUNTIME_OBSERVED` |
| journal/磁盘提交 | `UNRESOLVED` |
| CATALOG_READY_PASS | `BLOCKED` |
| ResourceManager/GameState/Step | `NOT_RUN` |
| no-VM/Android-free/训练 | `NOT_RUN` |

## 回归结果

本轮结束后重新执行全量专项回归：`531 passed, 1 skipped`，退出码 `0`，耗时
`5.15s`。这是工程测试结果，不改变真实 Android callback、catalog 或原生战斗生命周期门状态。

下一步仍是闭合真实 preference commit、双槽落盘、journal 和跨进程恢复语义，随后在新
子进程中重新验证自然 catalog 初始化；不通过门禁不推进战斗对象图。
