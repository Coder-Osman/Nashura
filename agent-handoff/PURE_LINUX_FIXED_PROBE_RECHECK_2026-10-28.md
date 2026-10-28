# E2.1-N：固定 probe 的真实 libg 重跑与配置计数审计修正

工作分支 `codex/pure-linux-libg-headless-20261026`；本轮起点
`cdc5fae77aa3ed5f1ac4eaeebebb07be8226792d`。本报告对应的最终交付 SHA
以 Git 提交为准。`main`、Android AVD、原 APK/libg/资源和历史证据保持原样。

## 结论

在现有 Ubuntu ARM64 guest 中，使用修复空 map 序列化后的真实 Android ELF probe，
重新执行了真实 `libg.so` 自然 loader。默认配置后端在双槽各写入一次后，因为
Backup 回调仍未解析，按失败关闭路径以 native exit 96 退出。显式选择的 Linux
文件系统队列候选 native exit 0，真实观察到 loader ready `0→1`，解析摘要为
162 张表、24,353 行，并正常销毁 loader；独立归档消费者 exit 0。

这属于 **RUNTIME_OBSERVED_LIMITED**。候选 loader 的 `provider_job` 包装仍 exit 1，
`configuration_contract_pass=false`、`catalog_ready_pass=false`。队列状态为本机
`PENDING`/归档消费，不是 Android BackupManager 等价交付。`CATALOG_TABLE_LOAD_COMPLETE`
标记未出现；表布局观察只证明计数及非空行指针，不证明单元格语义或完整资源等价。
因此本轮没有取得 `CATALOG_READY_PASS`，不进入 ResourceManager、GameState 或 Step。

## 修正的审计缺陷

首次重跑显示 native 候选写入后 `entries=[1,1]`，而调用方把空输入 XML 的
`[0,0]` 错当作预期输出计数，导致 `bounded_enqueue_observed=false`。native 进程
实际完成了 loader，队列也已消费；失败来自审计器的前置期望，而不是 native 运行。
保留首次结果及日志 SHA。随后将预期计数改为“每个输入 map 加入
`LOADING_SCREEN_STATE_LABEL` 后的键数”，并在全新 guest 目录重跑默认对照及候选。

计数器现在先严格解析输入 XML：根必须是无属性 `map`，条目必须是唯一键的
`string`，重复键及其他类型显式失败。预期值由集合并集计算，因此已有目标键不会
重复增加，无关条目保持计入。修复只改变观测期望，不更改 C store、数据或 Backup 行为。

## 实际结果

| 路径 | native exit | provider wrapper exit | loader ready | 双槽结果 | Backup 状态 |
|---|---:|---:|---|---|---|
| 默认未解析回调 | 96 | 1 | 未到达 | 2 次同步提交，`entries=[1,1]` | 1 次请求、1 次失败，保留失败关闭 |
| Linux 队列候选，首次旧计数 | 0 | 1 | `0→1` | 2 次同步提交，`entries=[1,1]` | 入队成功；旧审计期望错设为`[0,0]` |
| Linux 队列候选，修正计数重跑 | 0 | 1 | `0→1` | 2 次同步提交，`entries=[1,1]` | 候选解析通过，消费归档 exit 0 |

两次候选自然 loader 的表摘要一致：162 张表、24,353 行，布局 SHA
`8ff1043b9d3544dbd6cbefae576025845e1f5f6e8cb9a78a931428eb5de36f89`。
这不是逐 cell 对照。provider wrapper exit 1 是预期的严格门结果：Backup 服务等价性、
当前配置身份/异常域和完整 catalog 资源语义仍未闭合。

## 身份与隔离

- 宿主：Apple Silicon macOS；guest：`aarch64`，Linux `6.8.0-138-generic`。
- Android runtime：既有 API30 `linker64`/Bionic；未启动 Android Emulator 或游戏进程。
- APK SHA-256：`a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`。
- `libg.so` SHA-256：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 本轮 probe SHA-256：`a5640e59f07b5c2511450c5bebc92bc4c1d898da9c685aeb94cb09e7317b817d`。
- 当前配置材料 SHA-256：`e2d80e03f4beffe43301c91704a7a9b51141be2e89038a0754c5be7b98390883`。
- 两个初始 XML SHA-256 均为 `3325d2a819fdd8062c2cdc48a09b995c9b012915bcdf88b1cf9742a7f057c793`。
- 资源 manifest SHA-256：`47a96c006f5d56f25e6e350a26eab2dbef0b8dfb17fe33124188c85dc27fb108`。
- Asset backend SHA-256：`905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84`。

每次运行使用独立、权限受限的 guest 输出目录。probe、输入、auxiliary 资源与
资源 inventory 前后身份一致，`postcheck_errors=[]`。真实命令、每进程结果、
映射摘要及日志 SHA 见[机器证据](pure-linux-fixed-probe-recheck-evidence-20261028.json)。
私有原始日志、syscall、二进制和配置材料只保留在本机实验目录。

## 验证命令

probe 使用 NDK `27.3.13750724` 的 `aarch64-linux-android30-clang` 编译：

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread \
  -Wl,--export-dynamic tools/pure_linux/native_init_probe.c \
  tools/pure_linux/provider_probe.c tools/pure_linux/preference_store_hook.c \
  tools/pure_linux/preference_store.c tools/pure_linux/preference_files.c \
  tools/pure_linux/preference_contract.c tools/pure_linux/preference_codec.c \
  tools/pure_linux/preference_cipher.c tools/pure_linux/native_backup_queue.c \
  -ldl -o "$PRIVATE_GUEST_INPUT/probe"
```

真实运行沿用 `provider_job.py` 的 identity-bound 参数：`--mode both`、
`--factory-mode register`、`--catalog-mode loader`、`--inspect-parsed-tables`、
`--expected-preload-requests 377` 和只读 `--resource-trace`。默认对照不带
`--native-backup-queue`；候选显式带该参数。两者都使用同一组已核验 payload、
provider roots、资源 manifest、asset index/backend、当前配置材料和空 XML。
候选消费者只读取自己实验目录中的队列及两槽副本。

修正后完整专项命令：

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
```

最终结果见回执；测试只验证计数和审计工具，不替代上述原生运行观察。

## 资源回收与下一步

只停止了 guest 中两个经核实已暂停约20小时的旧 `native-probe-v27/v29` 进程；
它们的日志文件保留，SHA 记录在机器证据。Ubuntu guest 本身继续运行。

下一项仍是闭合 Linux Backup 候选与 Android 已观察请求、失败重排、payload/交付和
异常传播之间的契约，并取得真实资源语义/完整 catalog 证据。`PENDING` 队列和
loader ready 观察都不能单独开放严格门禁。GameState、Step、完整对局、Android
逐 tick 对照、无 JavaVM/Android-free 和 10× 均未运行。

[回执](CODEX_REPLY_2026-10-28_FIXED_PROBE.md)。
