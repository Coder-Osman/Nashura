# Codex回执：E2.1-N配置文件与平台审计

基线`506c7fd8551099815072ead66a6d5cb7d3298969`，当前分支
`codex/pure-linux-libg-headless-20261026`。本阶段提交SHA见本文件Git历史及最终交付消息。

- IMPLEMENTED：8份新源码/测试，真实文件原语、19组probe、双子进程job及10方法SHA绑定审计。
- UNIT_PASS：独立源码副本专项409通过/1跳过、主工程211通过、公开九组1,104通过/42跳过；三个入口均exit0，集合不累加。
- RUNTIME_OBSERVED：既有Ubuntu ARM64、Bionic/linker64、两个新child各19组/444项；build/wrapper/child全部0。
- STATIC_FOUND：真实Android30平台10方法，定位sync返回值未使用、bak删除返回值未使用及backup服务获取链。
- BLOCKED：严格catalog及ResourceManager；当前身份、XML/map、listener/queue和真实backup契约未闭合。
- NOT_RUN：新原语接libg、GameState、真正Step、完整局、Android逐tick对照、no-VM、10×和训练。

本轮输入187项未变；新增probe未加载libg/ART。保留首次提取失败、UTF8边界测试失败及旧版402项结果。
未改main、原始游戏输入、已有provider/probe、冻结部署或VM配置；未启动Emulator。
444项检查仅为合成文件原语，未取得原版SharedPreferences动态一致或战斗生命周期结论。

实际命令、环境、输入/输出SHA、私有日志哈希、未覆盖项和下一步见
[完整报告](PURE_LINUX_PREFERENCE_FILES_2026-10-28.md)及
[机器证据](pure-linux-preference-files-evidence-20261028.json)。
