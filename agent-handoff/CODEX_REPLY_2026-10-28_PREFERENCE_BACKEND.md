# Codex 回执：E2.1-N 双槽配置与 backup 边界

分支 `codex/pure-linux-libg-headless-20261026`，基线
`3ec4e6f7790f6485ac328629ad1be038bba12ae4`。阶段提交 SHA 由本文件 Git 历史及交付消息定位。

- IMPLEMENTED：15 份新增源码/测试，双槽 XML/string-map、历史 ASCII codec、原生 hook、证据解析与有界 backup 查询；修改 provider job 及测试。
- UNIT_PASS：独立源码副本专项 482 通过/1 跳过、主工程 211 通过、公开九组 1,104 通过/42 跳过；三个入口均 exit0，集合不累加。
- RUNTIME_OBSERVED：v116 两个 ARM64/Bionic child exit0、每 child 48 条历史参考一致；未加载 libg，不代表真实 backup 服务验证。
- RUNTIME_OBSERVED：v117/v118 四个真实 libg child 完成两槽 commit 后主动 exit96，wrapper exit1；backup callback 返回 -1，不冒充原版 BackupManager 动态失败。
- UNRESOLVED：v121 两个真实 NDK 服务查询 child 在 /dev/binder ENOENT 后 SIGABRT(-6)，没有查询返回阶段；不分类 SERVICE_ABSENT，也不等价于 Java 服务路径。
- BLOCKED：loading 配置的当前身份、XML/异常完整域及 backup 副作用；严格 CATALOG_READY_PASS=false，ResourceManager 未通过。
- NOT_RUN：GameState、空场/reset、真实 GameStateStep、动作、完整局、Android 逐 tick 对照、no-VM 生命周期、Android-free、10× 和训练。

187 项受保护输入未变，原始游戏文件、旧 probe/Provider 及冻结部署保持原样。
四个真实 libg child 各 188 实际映射已核验；本轮未重验全部 9,829 assets。
密码材料及其 SHA、XML 内容、原始日志和内存均不发布。248 份 tracked 历史 XML 保留原字节。
v115 collector 失败、v121 abort 及已有失败全部保留；不降低严格门。

下一步仅闭合配置写入平台契约，先利用既有 DEX/原版历史轨迹验证无服务及异常分支，
不安装 Binder、不默认返回成功、不越过 catalog 前置门。

实际命令、退出码、输入/输出 SHA、各 child 映射及日志引用见
[完整报告](PURE_LINUX_PREFERENCE_BACKEND_2026-10-28.md)和
[机器证据](pure-linux-preference-backend-evidence-20261028.json)。
