# E2.1-N：Backup pending 去重与 journal 分支

基线：`76ca520cae256b419eeaad177f3a47ac8a9b32e3`；分支
`codex/pure-linux-libg-headless-20261026`。本阶段保持 `CATALOG_READY_PASS=false`。

## 已完成及证据边界

上阶段用户批准的 loading 写入和恢复已经结束，两个 XML 恢复原字节。本阶段仅在
同一隔离 Android 30 ARM64 AVD 上只读检查 system_server，并审阅该实例提取的
services DEX。没有再次 store、调用 dataChanged、请求 Backup、重启游戏或修改队列。
没有重新加载 libg，也没有推进 ResourceManager/GameState/Step。

实际只读检查两轮各两次采样均得到：目标包属于 backup participants，目标请求已在
pending map 中，journal 对象存在。采样没有持队列锁，是当前状态摘要；四次结果相同
不构成原子快照，也不证明历史两次请求的瞬时状态或实际分支。

services DEX 中 dataChangedImpl 双参方法的40条指令及 catch table 经人工审阅；新增
工具将审阅结论绑定到精确方法 SHA，遇到方法变化则拒收。该工具不做自动语义证明。
公开报告只保留分支语义和身份，原始 DEX、反汇编、其他包元数据留私有。

## 静态正常路径

| targets | 含目标包 | put 返回旧请求 | queue put | journal 调用 | job schedule 调用 |
|---|---|---|---|---|---|
| null | — | — | 否 | 否 | 否 |
| 非 null | 否 | — | 否 | 否 | 是 |
| 非 null | 是 | null | 是 | 是 | 是 |
| 非 null | 是 | 非 null | 是 | 否 | 是 |

以上全部为 **STATIC_FOUND**。mQueueLock 覆盖 contains、put 及条件 journal 调用；
正常非 null 路径释放锁后调度 job，异常路径清理 monitor 并重抛。
journal 被调用不等于成功写入，schedule 被调用不等于任务交付。

因此，已有 pending 请求能解释 journal 的零计数，同时仍触发 schedule。该解释与当前
只读状态相容；历史事务是否实际命中该分支仍是 **UNRESOLVED**。
dataChangedTargets 还按调用者权限/UID 选择 participants；观察线程 UID1000 不代表
游戏调用者 UID，故没有主动调用该方法来制造游戏侧 targets 证据。

## 工具与真实运行

- `backup_queue_readonly.js`：精确核验 BackupManagerService/UserBackupManagerService，
  只输出目标包成员关系、数量和 journal 类型，不遍历其他包/文件内容。
- `run_backup_queue_readonly.py`：ADB cmdline 与 Frida 名称双检查；输出独占0600，
  故障留 JSON，退出时 detach。身份检查未包含 boot/starttime，不声称消除 PID 复用竞态。
- `backup_queue_contract.py`：DEX、dexdump、完整 dump 与规范化方法 SHA 绑定，
  私有输出目录0700，输入变更/工具失败/方法变化显式失败。
- 两份测试共25项通过；fake Frida 与合成方法仅验证工具边界，不是原生语义一致证据。

实际命令（路径仅指本机私有证据；已有输出保持原样，重验须换新目录）：

```sh
/tmp/nashura-frida-16.7.19-venv/bin/python tools/pure_linux/run_backup_queue_readonly.py \
  --adb /Users/gujiga/Library/Android/sdk/platform-tools/adb \
  --serial emulator-5580 --pid 522 \
  --script tools/pure_linux/backup_queue_readonly.js \
  --output /private/tmp/nashura-backup-queue-20261028-8KeLJO/inspect-fifth.json

/opt/anaconda3/bin/python3 tools/pure_linux/backup_queue_contract.py \
  --dex /private/tmp/nashura-backup-queue-20261028-8KeLJO/services.dex \
  --dex-sha256 66b315411f05fe53c5fb9f0e144857cef26dfce79927b004cb0e5a057a347a0a \
  --dexdump /Users/gujiga/Library/Android/sdk/build-tools/36.0.0/dexdump \
  --dexdump-sha256 9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692 \
  --output /private/tmp/nashura-backup-queue-20261028-8KeLJO/static-contract

/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider \
  tools/pure_linux/test_backup_queue_contract.py tools/pure_linux/test_backup_queue_readonly.py
```

三条最终命令均 exit0。前两次运行分别因 Frida getClass wrapper 和误猜 mService 字段
失败，退出码1，JSON原样保留；第三次 metadata 检查成功，但不是最终队列语义证据。
一次手抄 DEX SHA 错误被身份校验拒收，修正输入参数后成功，未放宽校验。
新增单测首次24通过/1失败，原因是fixture改成非DEX先触发magic校验；修正fixture后25通过。

## 回归与未解决项

隔离源码副本运行全量专项602通过/1跳过、主工程211通过，均exit0。源码副本与本轮
五个工具/测试文件SHA相同；原工作树250份历史XML未变。公开回归及发布检查结果、
输入/输出 SHA 和退出码见同阶段机器证据与回执，测试集合分别报告、不累加。

首要阻塞仍为 loading 配置及 Backup 的真实副作用契约与 Linux 绑定。历史事务的
queue put 旧值未捕获；journal/实际交付、fsync覆盖、完整配置异常域、跨进程恢复
仍有缺口。Linux 候选不得把 Backup 统一成功返回，也不得手动写 ready。

下一项最小实验是被动观察自然请求：在 dataChangedImpl 的同线程 scope 中按真实
pending-map 身份捕获 put 的原始返回，并关联 journal/schedule；没有自然请求就保留
NOT_OBSERVED，不主动发第二次请求或清 pending。随后依据证据实现最小回调并重跑
自然 catalog 初始化；严格门通过后才进入 ResourceManager 和 GameState。

本阶段未取得 no-VM、Android-free、完整对局或10×证据。
