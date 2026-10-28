# E2.1-N：运行时观测器恢复与闲置 AVD 清理（2026-10-28）

分支 `codex/pure-linux-libg-headless-20261026`，HEAD
`850c3673df6cd0fa5fde6919593562d9f0fd9919`。本记录只追加本轮工具恢复尝试和模拟器清理；
不覆盖此前 Backup、配置或 catalog 报告。

## 结果

- `nashura-e2-backup-api30` / `emulator-5580` 保持在线，未重启游戏、清除应用数据或更改
  AVD 配置。游戏 PID `6377`、`system_server` PID `522`；Backup Manager 当前显示 enabled。
- Frida host 17.24.3 的 `frida-ps -Uai` 能枚举设备应用，但 attach `system_server` PID 522
  失败，exit 1：`unable to connect to remote frida-server: closed`。设备临时目录中的
  Frida 17.24.3 ARM64 server 已退出；其日志记录 `frida_sigchain_compat_validate_signal`
  断言失败，不能将此次枚举解释为 hook 可用。
- 在临时 venv 安装 host Frida 16.7.19 / frida-tools 13.7.1 成功。官方 GitHub 16.7.19
  ARM64 server 下载在45秒内未收到数据，curl exit 28；中止自动重试时命令 exit 130。
  未取得该 server，因此没有启动旧版 server，也没有运行 JS hook。
- 闲置 `e2-1l-only` / `emulator-5686` 经独立 ADB 服务只读检查，系统停留在 Launcher，
  没有目标游戏进程或匹配 Nashura 进程。执行定向 `adb emu kill` 返回0，QEMU PID
  `77796` 随后退出。其 orphaned `netsimd` PID `77821` 保留，因可能被其他模拟器共享。
- 本轮没有观察到或排除配置 Backup callback；由于 hook 未安装，callback 运行时观测为
  `NOT_RUN`，不是 `NOT_OBSERVED`。未重跑真实 catalog。

## 命令与验证

| 操作 | 实际结果 |
|---|---|
| `frida-ps -Uai`（Frida 17.24.3） | exit 0，列出设备应用；不证明注入能力 |
| `frida -U -p 522 -l tools/pure_linux/backup_runtime_observer.js` | exit 1，remote server closed |
| 设备 Frida server 日志检查 | 观察到 signal compatibility assertion；server 进程不存在 |
| `pip install frida==16.7.19 frida-tools==13.7.1`（临时 venv） | exit 0 |
| 官方 server `curl`（45秒上限） | exit 28，0 bytes；手动中断重试 exit 130 |
| `adb -s emulator-5686 emu kill`（ADB server 5064） | exit 0；QEMU PID 77796 已退出 |
| `node --check tools/pure_linux/backup_runtime_observer.js` | exit 0 |
| 相关 pytest：backup audit/trace/method、preference contract/store | exit 0，74 passed |
| `git diff --check` | exit 0 |

文件身份：观测脚本 SHA-256
`5f9107af22b420e34462ecd81bdb748a49eafd8ce1cc0b6a6e58d5b4db1b0d27`；临时 Frida 16.7.19
macOS ARM64 wheel SHA-256
`17637bbe519d653a36f89d90d6eab5ad3f18c06302c8f5a3534df6ee9d8ea542`；设备上已存在的
Frida 17.24.3 ARM64 server SHA-256
`e187642b952189a9d5b69d2e787ee4dbe39df8dc15e7c42be09fa517cfd79e16`。下载失败的16.7.19
server 没有有效文件身份。

## 门状态与下一步

| 节点 | 状态 |
|---|---|
| 旧 AVD 清理 | `PASS`，仅关闭确认闲置的 `e2-1l-only` |
| 当前实验 AVD | `PRESERVED`，`emulator-5580` 仍在线 |
| Android 30 Frida hook | `BLOCKED`，17.24.3 server 崩溃，16.7.19 server 未取到 |
| Backup callback 服务端接收/同步结果/journal/job | `UNRESOLVED` |
| `LOADING_SCREEN_STATE_LABEL` 完整平台配置契约 | `BLOCKED` |
| `CATALOG_READY_PASS` | `false / BLOCKED` |
| ResourceManager / GameState / `GameStateStep` | `NOT_RUN` |

下一步唯一运行时阻塞仍是获得稳定的 API 30 服务端 hook 路径，以事务级关联真实客户端
配置写入、Backup 服务端处理、返回值与持久化/排程副作用。当前新旧 Frida 组合均未形成
可用 hook，因此不得重启当前应用来声称捕获已覆盖；应先取得匹配 server 或选择已有、
可验证且不改变服务状态的 instrumentation。工程测试通过不改变严格门状态。

本轮未修改 `main`、APK、游戏二进制、资源或 AVD 配置；未启动训练、GameState 或大规模
自博弈。此前已记录的 AVD 状态变更继续保留，当前实验 AVD 保持现场。
