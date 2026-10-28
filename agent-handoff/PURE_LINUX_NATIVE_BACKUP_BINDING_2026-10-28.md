# E2.1-N：原生 Backup 候选绑定与自然 loader 重复观察

基线 `13dfbe95ccf927ef710502f5b3a638f910c3e9a7`；分支
`codex/pure-linux-libg-headless-20261026`。交付 SHA 为包含本报告的提交。
保留历史 BLOCKED 记录，main、冻结部署、原 APK/libg/资源保持原样。

## 结论与边界

**RUNTIME_OBSERVED_LIMITED**：在现有 Ubuntu ARM64 guest 的三个独立
Bionic 子进程中，默认对照在两槽持久化后因 Backup 未解析退出96；显式启用
`native-filesystem-queue-v1` 的两个候选进程均退出0。两次均自然完成53次
loader step，观察到 ready 0→1、162张非空表和24,353行，随后析构及 dlclose。
每个候选退出后，独立消费者校验并归档两槽XML，消费者退出0，pending清除。

**BLOCKED**：这是自有原生文件系统归档后端的实际绑定证据。它没有证明与
Android BackupManager 的服务、调度、异常及交付语义等价。配置材料仍是历史
身份，`current_runtime_identity=UNRESOLVED`。三个严格包装器均退出1，
`configuration_contract_pass=false`、`catalog_ready_pass=false`，门禁保持关闭。

ResourceManager、GameState、空战场、reset、GameStateStep、完整对局和10×
均为 **NOT_RUN**。本轮没有启动 Android Emulator，没有更改 VM 配置。
有限子进程未使用 JavaVM，不登记为严格 no-JavaVM 冷启动验收。

## 实现

- `native_backup_queue.{c,h}`：显式启用的候选后端。0700目录、0600单链接文件、
  同进程同线程所有权、非阻塞独占锁；随机请求ID，fsync及原子发布pending。
  重复请求合并；损坏pending或残留staging显式失败并保留。成功只表示持久入队。
- `native_backup_consume.py`：写入方退出后取得 preference→backup 同序锁，
  校验两槽及恢复状态；写私有归档、fsync、重新读取验证后确认pending。
  失败保留证据并支持限定重试，不启动后台调度器，不宣称Android交付。
- `preference_store_hook.c`、`provider_job.py`：仅显式参数
  `--native-backup-queue` 且材料与双XML SHA齐备时接入。默认未解析路径保留。
  原版磁盘文件未修改；仅隔离子进程内既有版本绑定hook接入候选。
- `preference_hook_evidence.py`：严格解析一次绑定、一次请求及双commit；
  candidate与默认对照分别解析，所有整体gate固定保持false。
- `native_backup_binding_audit.py`：校验三个子进程、日志SHA、未变身份、
  原生退出码、生命周期标记、表布局、归档字节及pending确认；限定证据审计。

候选归档并非Android transport格式；不将现有Android fixture的正常/失败重排
结果迁移解释成这个后端的等价证明。异常注入测试属于工程测试。

## 实验与身份

宿主为Apple Silicon macOS，使用已有Ubuntu ARM64 guest，
`aarch64`、kernel `6.8.0-138-generic`。独立目录标签为
`nashura-backup-verified-20261028-w_y05ro4`，使用现有API30 linker64/Bionic。
原始命令、日志、XML、材料及二进制归档只保存在本机私有实验区。

| 输入 | SHA-256 |
| --- | --- |
| 用户APK | `a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f` |
| 真实libg | `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783` |
| runtime/update fingerprint | `e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339` |
| 最终probe | `74d6793ff4de288da3c4db7366b4ae2d498edf37b7069dc1321d5d22e1a47a31` |
| resource manifest | `47a96c006f5d56f25e6e350a26eab2dbef0b8dfb17fe33124188c85dc27fb108` |
| 历史配置材料 | `36f1ad056cb49ee68ba1f5d9947e0776b1518916cfbec75416d0d14e9a8fecf4` |

用户APK整包SHA与历史锁定APK不同，继续单独标记，不更改supported_engine或旧gate。
逐项核对187份输入及资源/auxiliary/probe前后身份，三进程postcheck_errors均空。
187是检查输入数，不是实际映射库覆盖数。其他身份和日志SHA见机器证据。

真实写入仅发生在独占实验目录内的双槽副本。原材料及两份初始XML的SHA不变；
没有追加Android游戏store，也没有用归档副本覆盖原值。

| 子进程 | native exit | wrapper exit | 自然ready | 消费者exit | 耗时秒 |
| --- | ---: | ---: | --- | ---: | ---: |
| 默认对照0 | 96 | 1 | 未达到 | NOT_RUN | 7.682 |
| 候选1 | 0 | 1 | 0→1 | 0 | 19.108 |
| 候选2 | 0 | 1 | 0→1 | 0 | 19.653 |

两个候选各产生两份1086bytes XML，归档与当前副本逐字节一致，重复进程亦一致。
表布局摘要SHA为 `8ff1043b9d3544dbd6cbefae576025845e1f5f6e8cb9a78a931428eb5de36f89`。
观察覆盖表布局、计数和非空行指针；没有逐cell内容或Android对照结论。
`table_load_completed=false`属于另一个未调用的direct batch入口，不代表本轮
自然loader未解析表。canary、析构、asset finish及dlclose标记正常。

保留linker DT警告、GMT/posixrules tzdata缺失和
`csv_logic/globalIdDb.csv` APK miss，未用默认资源掩盖。

## 实际命令与退出码

最终构建使用NDK `27.3.13750724` 的
`aarch64-linux-android30-clang`，退出0：

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread \
  -Wl,--export-dynamic tools/pure_linux/native_init_probe.c \
  tools/pure_linux/provider_probe.c tools/pure_linux/preference_store_hook.c \
  tools/pure_linux/preference_store.c tools/pure_linux/preference_files.c \
  tools/pure_linux/preference_contract.c tools/pure_linux/preference_codec.c \
  tools/pure_linux/preference_cipher.c tools/pure_linux/native_backup_queue.c \
  -ldl -o "$PROBE"
```

三个provider_job的完整实际argv已脱敏保存在机器证据 `commands`。
`$EXPERIMENT`、`$PAYLOAD`、`$RESOURCES`分别指新独占guest目录、已核验payload、
只读资源目录，保持真实参数与SHA。仅两个候选增加`--native-backup-queue`。
guest runner运行前设置`umask 077`；运行时使用真实strace并记录各child退出码。
batch harness退出0仅表示三个实验已执行，严格wrapper仍全部退出1。

每个候选退出后执行以下命令，实际退出均0：

```sh
python3 native_backup_consume.py \
  --directory "$EXPERIMENT/process-1/preference-store" \
  --output "$EXPERIMENT/process-1/consumer.json"
```

第二个候选使用process-2。真实证据下载后运行：

```sh
python3 tools/pure_linux/native_backup_binding_audit.py \
  --experiment "$PRIVATE_EXPERIMENT/evidence" \
  --output "$PRIVATE_EXPERIMENT/binding-audit.json"
```

该限定审计退出0，审计文件SHA
`ccd99d54682f67c2fd6a71de3fd6cc33f51369f690019bf8045a1a5dd7864b74`。
batch SHA `690811acf4c6a95417bdd8cd2bf505c41abeaa447f3c6f47d9f59f0a7c92da22`；
私有evidence归档SHA `fafa190405b68c51b51a17601b10b89c6d054bc579ebd8e782995bbd17ae8021`。
公开仅摘要及SHA，不发布映射地址、syscall原文、配置或二进制。

## 独立测试与失败保留

本轮新增43项：队列/消费者18项、候选解析15项、证据审计10项。
合成fixture与fault injection仅验证工具契约，独立于上述真实libg结果。
在独立仓库副本执行以下命令，避免覆盖历史XML：

```sh
python3 -m pytest -q -p no:cacheprovider tools/pure_linux
python3 tools/run_main_engine_checks.py
python3 tools/run_public_checks.py
```

结果：专项900通过/1跳过，主工程211通过，公开九组1104通过/42跳过，三组均退出0。
测试集合分别报告，不将互相重叠的集合累计成独立测试数。
源码清单、凭据扫描和原历史XML身份核验结果见机器证据。

保留以下失败：

1. 首次队列host编译缺少getentropy声明：58通过/1跳过/18错误；
   补`sys/random.h`后验证通过，没有改变运行期策略。
2. 最终probe的首个批次`nashura-backup-final-20261028-bvo2dat7`三个进程均96：
   material副本权限0644，在安装backend前被拒收。保留该批次；
   新目录使用`umask 077`，严格权限检查原样保留，随后取得上表结果。

## 首个未闭合依赖与下一步

首个门禁阻塞仍是**完整配置契约**：历史材料需绑定当前runtime，且Backup与
配置异常域的原生实现需取得真实Android语义对照。这个文件归档候选解除的是
限定实验中的退出96，不解除严格catalog门禁。

下一步先逐项对照候选与真实Android请求、持久化、重复、失败重排和交付语义，
明确宿主归档契约与平台差异，取得当前材料身份；继续保存所有失败和未知。
严格CATALOG_READY_PASS前保持后续对象图NOT_RUN。

机器证据：[JSON](pure-linux-native-backup-binding-evidence-20261028.json)。
回执：[CODEX_REPLY](CODEX_REPLY_2026-10-28_BACKUP_BINDING.md)。
