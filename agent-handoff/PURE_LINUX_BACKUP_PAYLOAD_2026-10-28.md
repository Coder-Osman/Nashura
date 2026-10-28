# E2.1-N：非空 Backup payload 与真实队列消费

日期：2026-10-28。基线 `0ad2e5a04fe3abd6eb7e977184418cf5170b4c9d`。
分支 `codex/pure-linux-libg-headless-20261026`；交付身份为承载本报告的 commit。

## 结论

本轮为 **RUNTIME_OBSERVED_LIMITED**：专用 Android ARM64 实例的自有 fixture
完成非空 Backup、原 agent 正常返回及后续真实队列消费。Linux Backup binding
仍 **UNRESOLVED**，严格 `CATALOG_READY_PASS=false`。本轮未加载 libg、未执行自然
catalog、ResourceManager、GameState 或 Step；没有无 VM、完整战斗或性能结论。

用户授权的一次真实游戏 store 与原值恢复已在前阶段完成，本轮未重复。
以下所有写入均针对自有包 `org.nashura.backuppayload`，专用实例没有安装游戏。
原 APK、libg、Provider、update、旧任务和 main 保持原样。

## 实现与证据边界

新增 fixture manifest、Receiver 和 BackupAgent 三文件：Receiver 只接受一次带 token
的同步 SharedPreferences commit，然后调用真实 BackupManager.dataChanged；已有 token
时显式失败。Agent 调用真实 SharedPreferencesBackupHelper，记录原方法进入和正常返回。
没有 system_server hook，没有模拟 transport 成功。

新增 `run_backup_payload.py`、`backup_payload_audit.py`、`test_backup_payload.py`。
runner 在专用 AVD、无游戏、平台/APK SHA 和 system_server 身份核验后执行，不重试；
独占输出目录并保留失败结果。审计器绑定三个输入 SHA、56份命令输出的 stdout/stderr
SHA、命令顺序与白名单、一次 store/request/backupnow、身份、队列和文件字节。
37项工程测试包含合成 fixture、26种破坏证据和归档/超时/独占输出拒收。
这些测试验证工具约束，不代表原生游戏语义一致。

## 环境与输入

专用实例 `nashura-e2-backup-contract-api30 / emulator-5582`，Android 11 ARM64。
fingerprint、APK/platform、源码及私有归档 SHA 见机器证据。
JavaVM 和原 Android Backup 服务保留；使用 LocalTransport。
fixture APK SHA `fd8106ccb065255550498ba2b36e9323ac9db7cfb7535d93c6362dce431e7d5a`。
services.jar SHA `c6992cdc2a3e23d5aeb6ab77e7a7f6889cfcf5c262c6ed86825e6fcbf601681d`。
本轮没有重新加载或核验真实游戏全套输入，不把历史游戏 SHA 写成本轮运行证据。
APK、签名密钥、token、原日志与设备私有文件均留在私有目录，仅发布摘要和哈希。

## 实际结果

1. 一次自有 fixture store/request，随后一次 `bmgr backupnow --non-incremental`。
   worker PID470/TID5454，agent PID5413/TID5431，原 agent 正常返回；
   日志 `backup_package` 为261bytes，LocalTransport报告非增量备份。
   **直接备份完成后目标仍 pending，journal 仍包含目标**。
2. 没有追加 store、dataChanged 或 backupnow；仅一次 `bmgr run` 消费已排队工作。
   worker PID470/TID5679，agent PID5413/TID5430，原 agent 正常返回；
   未变化增量的日志数据量0bytes。pending总数5→2，目标消失。
3. 两次消费之间落盘 transport233bytes、prefs XML217bytes、agent state72bytes
   逐字节相同；transport偏移16之后等于整个 XML。16bytes header语义仍 **UNKNOWN**。
   日志流261bytes与落盘233bytes是不同量纲，不混用。
4. 含目标的一份 journal（133bytes）在之后列表中消失。只读 followup读取四份
   存活 journal：目标条目均0，保留文件字节未变；新增 journal不含目标。
   这是文件前后观察，**journal删除方法调用次数 UNKNOWN**，不是动态方法计数。
5. 完成后APK、平台、system_server PID/starttime/boot身份保持一致。

自然 job 到期、失败/重排/崩溃恢复路径均 **NOT_RUN**；工作线程仅日志覆盖，
没有 transport 方法计数或跨线程严格因果 trace。上述成功分支不闭合全部服务契约。

## 失败记录与验证

首次构建因 class 文件 glob 展开时机导致 d8 未产出 classes.dex，第四条 aapt 命令
exit1；原记录保留。build-v2七条构建命令均exit0，源文件与发布字节一致。
加强审计时首次未接受 Android 安装路径中的 `~`，离线审计exit1；修正后 v3/v4
exit0。旧失败与输出均保留，未为修复审计重复触发 Android 请求。

实际执行命令由机器证据列出 argv/退出码与私有归档 SHA；脱敏重验形式：

```sh
python tools/pure_linux/backup_payload_audit.py \
  --run "$PRIVATE_RUN/observe/result.json" --run-sha256 "$RUN_SHA" \
  --drain "$PRIVATE_RUN/drain/commands.json" --drain-sha256 "$DRAIN_SHA" \
  --followup "$PRIVATE_RUN/journal-followup/result.json" --followup-sha256 "$FOLLOW_SHA" \
  --output "$EXCLUSIVE_OUTPUT"
python -m pytest -q -p no:cacheprovider tools/pure_linux
python tools/run_main_engine_checks.py
python tools/run_public_checks.py
```

离线审计exit0。独立源码副本专项 **810 passed / 1 skipped**、主工程 **211 passed**、
公开九组 **1104 passed / 42 skipped**，三条命令均exit0；集合独立，不累计为额外测试。
252份原工作树XML逐SHA未变。发布前另做清单、凭据、差异检查，见发布核验JSON。

## 资源释放与下一步

关闭前核验5582的AVD名、host PID21461/启动时间/命令和无游戏安装；仅执行该实例
`emu kill`，exit0。随后PID已退出、ADB列表仅剩受保护5580。5580及Ubuntu VM未操作。

当前前置阻塞仍是 **Linux Backup服务副作用的明确所有权及实际绑定**：pending、
journal、scheduler、交付/失败重排需完整契约；本轮补上正常非空交付和队列消费观察，
没有消除异常域缺口。`preference_store_hook.c::unresolved_backup`继续失败关闭。
下一步定位真实DEX的失败、重排和journal所有权边界，再定义最小可验证Linux契约；
契约接入真实libg且自然catalog严格通过后，才推进后续对象图。
