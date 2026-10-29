# E2.1-N：Linux 原生持有者设置阶段

日期：2026-10-29。基线 `3fbb164`，真实引擎 SHA-256 为
`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
本阶段实现了版本绑定的 holder setup 入口、输出契约和独立审计器，但真实
Ubuntu ARM64 guest 登录凭据在当前会话不可用，因此阶段状态为
`BLOCKED_ENVIRONMENT`。

## 已验证

Android 匹配库的独立启动追踪稳定完成：事件数6056，`errors=[]`、`lost=0`，
`owner_holder_setup_candidate` 覆盖，设置入口 `0x139e330` 的槽位从空变为非空，
随后同线程的 owner 初始化事件以 `holder_setup_id=2` 关联。update 清单前后均为
908 项，SHA-256 均为 `47d4ad200eafff21ea1538132e2bb629526efd87e495d72baf3acb9d95f9e2bc`。
机器结果文件 SHA-256 为 `d7b8c2549548de2d23d0ec3cbe3149cd484071d623d7c304655dde045f749f01`，
追踪脚本 SHA-256 为 `c03f5bf0f77acaba856deb5fad44b413cd84a05d09da46c6f3316fee8208c90e`。

Linux 侧已加入 `--holder-setup-only`、对象槽/分配器关系检查、析构清理和
`HOLDER_SETUP_OBJECT_OFFSET=0` 输出；入口 guard 为 `libg.so+0x13a3e3c`，
guard SHA-256 为 `78cc9be1bb5f8c5b2b5d8126531866d5beef174720d0a854e8cdaa899133dd02`。
交叉编译得到的 ARM64 probe SHA-256 为
`35dc410dd8cb35890d4cdeeaaa798f85129ee8ee6f4fce0366d12b70180a30c8`，但该 probe
没有在 guest 中执行。

## 环境阻塞证据

VMware Ubuntu 22.04.5 ARM64 guest 仍在运行，VMware Tools 状态为 running。对同一
guest 执行 `listProcessesInGuest` 和 `runProgramInGuest` 时，`vmrun` 返回：
`Error: Command requires valid user name and password for the guest OS`。guest 的
SSH 22 端口连接被拒绝。由于没有可验证的 guest 命令执行通道，本轮没有生成
`result.json`、`stdout.log` 或 `stderr.log`，也没有调用 `provider_job.py` 的
`--holder-setup-only` 实验。

## 门与限制

| 项目 | 状态 |
|---|---|
| Android holder setup 追踪 | `PASS` |
| holder setup 实现、审计器及单元测试 | `UNIT_PASS`；纯 Linux 专项 `1105 passed, 1 skipped` |
| Linux 真实 holder setup | `BLOCKED_ENVIRONMENT`；`NOT_RUN` |
| `CATALOG_READY_PASS` | `false` |
| ResourceManager、GameState、真实 Step、完整对局、性能 | `NOT_RUN` |

本阶段不声称 Linux holder setup 已通过，也不改变运行中的 VM 或冻结部署。下一次
具备 guest 登录通道后，应在隔离目录运行相同版本的 probe 和
`provider_job.py --holder-setup-only --timeout 120`，回收三份日志后再运行
`audit_native_holder_setup.py`；若输入身份、输出顺序或清理门不满足，仍保持
`BLOCKED`。
