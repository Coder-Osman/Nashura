# Codex 回执：catalog 内层查询

- `RUNTIME_OBSERVED_LIMITED`：匹配的 908 项 update、30 字节查询键和嵌套作用域
  已审计；Android 内层返回非空，Linux 返回空。
- `UNIT_PASS`：`python -m pytest -q -p no:cacheprovider tools/pure_linux`，
  exit 0，1,036 passed、1 skipped；`node --check` exit 0。
- `BLOCKED`：查询容器的内容与填充来源、配置异常和 Backup 等价仍待验证；严格
  catalog 门尚未通过。
- `NOT_RUN`：ResourceManager、GameState、Step、完整对局及 10 倍性能实验。

详细命令、输入 SHA、失败采样及限制见
[阶段报告](PURE_LINUX_INNER_LOOKUP_2026-10-29.md)和
[机器证据](pure-linux-inner-lookup-evidence-20261029.json)。报告基线为
`d660b8a6694c211671a6feb0d7aeeee88676079a`；发布提交由 Git 历史标识。
