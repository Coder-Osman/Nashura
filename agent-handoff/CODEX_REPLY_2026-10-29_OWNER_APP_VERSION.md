# Codex 回执：限定版本桥与对象构造

- `RUNTIME_OBSERVED_LIMITED`：Android 匹配包返回版本 `15.535.13`；Linux 使用限定版本桥后，真实对象构造和获取入口返回，字符串字节与对象虚表检查通过。
- `UNIT_PASS`：相关测试 1082 passed、1 skipped；跨运行身份审计 exit 0。
- `BLOCKED`：严格 catalog、配置异常和 Backup 等价，及真实上游对象生命周期。
- `NOT_RUN`：ResourceManager、GameState、Step、完整对局、无 VM 生命周期及性能验收。

实际命令和限制见[报告](PURE_LINUX_OWNER_APP_VERSION_2026-10-29.md)及[机器证据](pure-linux-owner-app-version-evidence-20261029.json)。提交身份以 Git 历史为准。
