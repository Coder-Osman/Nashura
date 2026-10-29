# E2.1-N：Linux holder setup 的真实执行与 JavaVM 阻塞

日期：2026-10-29。该报告承接 `PURE_LINUX_HOLDER_SETUP_2026-10-29.md`，记录
在恢复 guest 登录通道后对同一版本、同一资源身份和新 probe 的真实执行结果。
旧报告中的 `BLOCKED_ENVIRONMENT` 保留为历史状态；本报告把状态推进为
`RUNTIME_BLOCKED_JAVAVM`。

## 结果

Ubuntu ARM64 guest 中的 `provider_job.py --holder-setup-only` 已实际启动并完成
内容 root、filesystem、data/update provider 注册。holder setup 入口随后完成
分配器和首个对象分配，但在 Java bridge 的空 `JavaVM` 解引用处收到 `SIGSEGV`：

| 观测 | 结果 |
|---|---|
| `libg.so` | SHA-256 `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| 本轮 probe | SHA-256 `d4fc8504bb00ec67775bdc826973661893968226ec73ecf3355e2293cb6b8216` |
| 内容 root / filesystem | 返回；`CONTENT_READY=0`，filesystem 对象 392 字节 |
| data/update provider | 两个注册返回，非零槽位和 vtable 校验通过 |
| holder setup 输入 | `HOLDER_SETUP_INPUT_READY=1`，`SLOT_BEFORE=0` |
| allocator / 首次分配观测 | `libg.so+0x13a3e58`、`libg.so+0x13a3e7c` 各 1 次 |
| Java bridge | `libg.so+0x140b31c` 进入 1 次 |
| 首个 fault | `libg.so+0x17d8b14`，`x0=0`，`SIGSEGV` |

本轮没有写入 ready、伪造 `JavaVM` 或继续调用未知初始化入口。`JNI_OnLoad`
仍只被 `dlsym` 解析，没有被 probe 显式调用；因此 `JavaVM` 全局没有通过真实
Android loader 生命周期绑定。guest 只读运行时检查同时确认没有 `/system`、`/apex`、
`app_process64`、`dalvikvm64` 或 `libart.so`，所以当前 ARM64 Linux guest 没有可
直接复用的真实 ART/JavaVM。

## 证据身份

本轮 Linux 结果和 GDB 观测位于私有隔离目录，公开仓库只保存摘要及哈希：

- `result.json`: `de034567b90a178b7a8a3f17c5c53125f0f1f81b911da7a42463d3e86c440769`
- `stdout.log`: `8740d97d8272b422cf08852adff87de84c34d9c6766ebee61324b7a1018d1f4e`
- `stderr.log`: `6ce8eab3c17840393c48df62302edca7f786c3d1a861ac0b57b8d372bf3b533c`
- `observation.json`: `b7ef7e6d51289480f216dbf4696f48010dca1b6578c946a0875c91d6c397283e`

输入、资源、辅助文件和 probe 的前后身份均一致，`postcheck_errors=[]`。完整机器
字段见 [机器证据](pure-linux-holder-setup-javavm-block-evidence-20261029.json)。

## 门状态

| 项目 | 状态 |
|---|---|
| Android holder setup | `PASS`（既有稳定追踪） |
| Linux holder setup 真实执行 | `RUNTIME_BLOCKED_JAVAVM` |
| `CATALOG_READY_PASS` | `false` |
| ResourceManager / GameState / Step / 完整对局 | `NOT_RUN` |

下一步应在真实 Android 进程内复用 `System.load` 已建立的 JavaVM，或提供完整、可
审计的 ART 运行时闭包后再重试 Linux setup。当前 guest 的纯 Linux 路径不应通过
空指针默认值、伪造 ready 或伪造 JavaVM 推进到 catalog 或 GameState。
