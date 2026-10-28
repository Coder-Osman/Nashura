# E2.1-N：当前配置身份与真实单事务字节对照

基线 `49ccae96835da7a6db2b1aa6cf035bf9f7cc21fa`；工作分支
`codex/pure-linux-libg-headless-20261026`。交付 SHA 为包含本报告的提交。
历史失败及 BLOCKED 原样保留；main、冻结部署与原 APK/libg/资源保持原样。

## 结果与限定范围

**RUNTIME_OBSERVED_BOUND**：绑定当前隔离 Android 游戏进程的配置材料、
六个 Cipher 状态、双槽文件、APK/DEX/libg 与三个真实 ARM64 libg 子进程。
当前 o 材料与历史不同，p 和 IV 相同；历史材料没有被冒充为当前材料。
只读捕获两次一致，未调用原 Cipher、store 或 Backup，材料保存在私有目录。

**单事务持久化字节对照通过**：用户授权的一次目标 store 和一次恢复已经完成。
目标为 loading key 的 ASCII 值，初始双槽均为空 map。Android 双槽同步 commit
及 Backup 顺序由原路径观察，恢复后两槽 XML 与写前完全相同，APK/libg/进程身份不变。
不追加真实 Android 写入或 Backup 请求。

ARM64 Linux/Bionic 两个独立进程使用当前材料，在独占空 map 副本中执行相同事务；
写入、关闭重开读回、恢复通过。修复后两进程×两个阶段×两槽共8项，XML逐字节
与 Android 一致。目标槽各208bytes，恢复槽各65bytes。

**BLOCKED**：Android Backup 服务等价性和配置异常域尚未闭合。
Linux 重放的 Backup 回调显式返回未解析，两次 write 均返回 -1；双槽 commit
先于这个失败发生。重放退出0仅代表探针预设的局部条件满足，没有加载 libg。
`configuration_contract_pass=false`、`catalog_ready_pass=false`，不开放后续对象图。

## 真实 libg 集成边界

三进程使用当前材料与空 map 副本：默认对照 native96；两个显式文件系统归档候选
native0、独立消费者0；三个 strict wrapper 全部1。候选自然 loader ready 0→1，
162张表/24,353行，canary、析构、finish、close正常。
这只是候选后端限定观察，未证明 Android Backup 等价。

三个 libg 进程使用修复空 map 格式前的 probe，SHA 为
`74d6793ff4de288da3c4db7366b4ae2d498edf37b7069dc1321d5d22e1a47a31`。
本次格式修复由新的 Bionic 重放 probe 验证，SHA 为
`071e23667d03638293ab0f7ea40703203b17369eaebacd23e3387a94b572671b`。
未将旧完整 probe 的执行归于新源码。嵌套旧审计中的 identity=UNRESOLVED
保留其旧含义；顶层审计单独绑定当前材料，且不覆盖整体配置门。

ResourceManager、GameState、空战场、reset、GameStateStep、完整局、训练、
无 JavaVM 冷启动验收和10×均未执行。

## 实现与发现的真实差异

- `preference_runtime_readonly.js` 与 runner：只读捕获并核对当前材料、Cipher、
  locale/charset、双槽、安装路径和进程；私有输出与最终完整性检查。
- `preference_runtime_replay.c`：显式材料与独占目录，重开验证持久化，O_EXCL
  发布阶段快照；异常和 Backup 保持未解析，不统一返回成功。
- `runtime_preference_binding_audit.py`：校验捕获、原事务、真实 libg 批次和
  局部重放之间的身份、输出 SHA、参数及失败边界。
- 旧审计新增可选 expected_entries，默认仍为历史 [12,12]；新审计先证明
  初始空 map，再明确使用 [1,1]，旧 gate 和历史 CLI 行为保持原样。
- `preference_store.c`：空 map 序列化使用 Android 实测的 `<map />`。

修复前负证据批次 `nashura-runtime-codec-replay-20261028-4ZjVzkU9`：
target 4项字节一致，restore 4项仅 map 一致。原生70bytes，Android65bytes；
差异为 `<map>\n</map>` 与 `<map />`。修复后使用新目录
`nashura-runtime-codec-replay-fixed-20261028-dvLIeyIQ`，8项全部一致。
负证据审计SHA `518a9f5d6ddc736c1e08753a88c18e3504e26a4dfac1e7f75733bd0605a581cd`。

## 环境与实际命令

宿主 Apple Silicon macOS；已有 Ubuntu ARM64 guest，aarch64，
kernel `6.8.0-138-generic`。使用既有 Android API30 linker64/Bionic/libcrypto。
未安装新组件、修改 VM 配置或操作其他任务。原始日志、XML、材料、资源和二进制
只保存在私有实验目录；公开仅代码、摘要与 SHA。

以下变量表示实际命令中的私有绝对路径。编译及两次 guest 重放均 exit0：

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread \
  tools/pure_linux/preference_runtime_replay.c \
  tools/pure_linux/preference_codec.c tools/pure_linux/preference_cipher.c \
  tools/pure_linux/preference_store.c tools/pure_linux/preference_files.c \
  tools/pure_linux/preference_contract.c -ldl -o "$PRIVATE_PROBE"
"$PAYLOAD/linker64" "$EXPERIMENT/probe" "$PAYLOAD/libcrypto.so" \
  "$EXPERIMENT/material.bin" "$EXPERIMENT/process-0" "$TARGET"
```

编译器为 NDK27.3.13750724 的 aarch64-linux-android30-clang；第二次使用 process-1。
两次重放的输入和 runtime SHA 不变，VMTools 六步均0，不用该返回值代替 native退出码。
三个真实 libg 批次的最外层 run.exit 为空，保留这一归档缺口；native/wrapper/consumer
退出码由独立结果记录，不将空文件解释为0。

限定审计实际 exit0，参数指向上述私有归档，输出采用独占新文件：

```sh
python3 tools/pure_linux/runtime_preference_binding_audit.py \
  --capture "$CAPTURE" --transaction "$TRANSACTION" --native "$NATIVE" \
  --replay "$REPLAY" --output "$EXCLUSIVE_OUTPUT"
```

输入/输出及日志 SHA 见机器证据。当前材料仅发布摘要SHA，不发布其字节或设备标识。
用户APK整包SHA与历史锁定APK不同，继续单列，不修改 supported_engine 或历史 gate。

## 工程验证与失败记录

125项定向测试通过；合成 fixture 验证审计拒收边界，独立于真实事务证据。
全量检查在独立仓库副本执行，保护原历史XML。首轮系统 Python3.9 缺 cryptography，
且主工程需要 Python3.10+ 的类型/slots 特性；专项及两个工程检查均 exit1。
失败日志保留，随后使用已有 Anaconda Python3.13 重跑，不修改依赖配置。
Python3.13最终专项951通过/1跳过、主工程211通过、公开1104通过/42跳过，
三组均exit0。253份原仓库XML的SHA逐项不变；实际命令与日志SHA见机器证据与回执。

## 下一步唯一门禁阻塞

完整配置契约仍未闭合：优先对齐 Android Backup 请求/去重/失败重排/交付与
原生候选的所有权、持久化及异常传播；当前空 map 单个 ASCII 事务不覆盖损坏密文、
混合键、其他 locale、断电或跨进程 Binder 因果。严格 CATALOG_READY_PASS 前，
保持 ResourceManager 和 GameState/Step 未运行。

[机器证据](pure-linux-runtime-preference-binding-evidence-20261028.json)；
[回执](CODEX_REPLY_2026-10-28_RUNTIME_PREFERENCE.md)。
