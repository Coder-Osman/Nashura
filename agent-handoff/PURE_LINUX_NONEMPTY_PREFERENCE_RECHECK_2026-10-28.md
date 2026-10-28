# E2.1-N：非空偏好编码键与真实 libg 重跑

分支 `codex/pure-linux-libg-headless-20261026`，基线
`b6f92a23f8ca968abf06e26ddcb9a1d0969abd1e`。本阶段修正非空偏好表的条目数
推断，并在隔离 Ubuntu ARM64 guest 中以真实 `libg.so` 重跑一个已有目标键的候选。
此前报告与失败记录保留，严格配置和 catalog 门均未放开。

## 问题与修复

`provider_job.preference_write_entry_counts` 原先计算 XML 键集合与明文
`LOADING_SCREEN_STATE_LABEL` 的并集。但原生配置实现先将该键按每个槽的密钥做
AES-256-ECB、PKCS#7 和 Base64 编码，再用编码结果查找 XML。空表的预期值 `[1,1]`
正确；当编码键已存在时，旧算法误将它当成新键，可能把实际 `[1,1]` 判为 `[2,2]`。
旧算法相关的明文键单元测试并未覆盖真实存储契约。

新实现对非空表使用本次显式 SHA 绑定的 80 字节材料，分别计算两个槽的编码键，
只返回写后条目数，不输出材料或编码键。缺材料、对应槽不可用或没有 Python
`cryptography` 时记录未知，审计器保留实际摘要且拒绝确认 bounded enqueue。
材料长度错误、重复键及非法 XML 显式拒绝；空表继续按 `[1,1]` 检查。
这些计数只对应非空目标值写入，现有观察器仍要求 `deleted=false`、双槽 commit
及 Backup 请求，不据此声称完整配置等价。

两组固定 AES 向量分别用系统 OpenSSL 独立计算。已有 Android/Bionic 字节对照的
两份目标 XML 与材料绑定后，空表预期 `[1,1]`，已有编码目标键的表预期也为
`[1,1]`。这是输入和编码身份的局部验证，不是完整配置异常域验证。

## 真实运行

- guest：`aarch64`、Linux `6.8.0-138-generic`；实际使用既有 API30 Bionic/linker。
- 真实 `libg.so` SHA-256：
  `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- probe SHA-256：`a5640e59f07b5c2511450c5bebc92bc4c1d898da9c685aeb94cb09e7317b817d`。
- 配置材料 SHA-256：`e2d80e03f4beffe43301c91704a7a9b51141be2e89038a0754c5be7b98390883`。
- 两个输入 XML SHA-256：`61cd030b3e1dea7cb38df471ec520d10c34ee9be99dbee87777ff2e534428aec`、
  `85cfe0ee75d8b520f4aa67ee26846bd928883aba7ab9fb88441ae7b32281b96f`；它们是既有
  对照实验的非空目标快照副本，不是对用户原始游戏文件的写入。
- provider 脚本 SHA-256：`a82bd2066fc44a199a6042424208bdce6d114a09f4d2285bd57d452dac630aa3`。

在新目录 `/tmp/nashura-nonempty-probe-EYVOkQ` 中使用既有输入和资源 manifest，
`provider_job.py` 以 `--mode both --factory-mode register --catalog-mode loader
--inspect-parsed-tables --expected-preload-requests 377 --native-backup-queue`
运行；材料和两份 XML 均显式提供路径及上述 SHA。使用真实 `strace` 包装。
目标值更新后双槽条目数均为 1，两个 commit、两次磁盘写和一次 Backup 候选入队
均观察到；候选入队状态 `PENDING`，Android 等价性仍为 `NOT_VERIFIED`。

原生子进程 exit 0，外层严格 wrapper exit 1；没有超时，输入及资源前后身份一致，
`postcheck_errors=[]`。loader 自然观察到 ready，解析布局摘要为 162 张表和 24,353 行；
布局 SHA `8ff1043b9d3544dbd6cbefae576025845e1f5f6e8cb9a78a931428eb5de36f89`。
这只是指针/计数布局，不证明各 cell 或 Android 资源语义一致。直接 table-load 路径
本次未调用，相关标记不存在不代表自然 loader 没有执行。

VMware Tools 命令的 exit 0 只表示调用返回，native/wrapper 退出码分别来自进程内
采集的结果。runner SHA `3a6ea141268be08e647e10582281d1ff31e4c741ae39a1928889dcc2b76beee5`；
原始私有 runner、日志和材料保留在隔离目录，不进入公开仓库。摘要及日志 SHA 见
[机器证据](pure-linux-nonempty-preference-recheck-evidence-20261028.json)。

## 测试与门禁

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0：957 passed, 1 skipped
```

固定密钥向量、跨槽误配、无加密库、错误材料长度和未知预期均有定向测试。
`tools/run_main_engine_checks.py` exit 0，211 passed；`tools/run_public_checks.py` 在
隔离副本 exit 0，1,104 passed、42 skipped；源码清单 2,586 项通过。
首次 gitleaks 将两个公开的固定测试向量变量名误判为 API key，重命名变量后复扫
exit 0、无发现；测试向量字节没有变化。历史 XML 未因公开回归被覆盖。
`configuration_contract_pass=false`、`catalog_ready_pass=false`；Backup 的实际交付、
失败恢复和 Android 语义等价性以及完整 catalog 资源语义仍未闭合。ResourceManager、
GameState、真实 Step、完整对局、无 JavaVM 验收及 10× 性能测试均为 `NOT_RUN`。
下一步继续针对 Backup 所有权/异常传播与 catalog 内容语义取得可比运行证据，
通过严格门后再推进战斗对象图。
