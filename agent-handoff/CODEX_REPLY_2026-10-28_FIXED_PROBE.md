# Codex 回执：固定 probe 真实 libg 重跑

分支 `codex/pure-linux-libg-headless-20261026`，起点 `cdc5fae77aa3ed5f1ac4eaeebebb07be8226792d`。
交付 SHA 由承载本回执的 commit 确定。`main` 未变。

- **IMPLEMENTED / UNIT_PASS**：配置审计器从输入 XML 推导目标 key 写入后的条目数；重复键和非 string XML 拒收。
- **RUNTIME_OBSERVED_LIMITED**：真实 ARM64 `libg.so` 默认路径两槽提交后遇 unresolved Backup，native exit96；配置汇总为`entries=[1,1]`。
- **RUNTIME_OBSERVED_LIMITED**：显式 Linux 队列候选 native exit0；自然 loader 完成并观察 ready `0→1`；表布局摘要162张表/24,353行；消费者 exit0。
- **BLOCKED**：provider wrapper仍exit1；候选Backup只是本机pending/归档，不证明Android服务等价；严格配置与catalog门均false。
- **NOT_RUN**：ResourceManager、GameState、Step、完整局、Android逐tick对照、无VM/Android-free和10×。
- 首次审计把`[0,0]`输入计数误用为写后期望，已保留该次结果；修正后在新的独立目录重跑。
- 两个约20小时前暂停的旧probe进程在核对身份后停止；输出日志保留。

输入 SHA、每次 native/wrapper 退出码、结果及日志 SHA、队列回执和映射覆盖见
[机器证据](pure-linux-fixed-probe-recheck-evidence-20261028.json)。本轮没有启动 AVD、
执行新的 Android store/Backup 请求或触碰其他运行任务。

下一步继续补齐 Backup 异常/失败交付契约和完整 catalog 资源语义证据；当前尚未满足
严格 `CATALOG_READY_PASS`，因此后续原生战斗对象图仍保持关闭。
