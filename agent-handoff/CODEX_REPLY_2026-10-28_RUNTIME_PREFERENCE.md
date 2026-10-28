# Codex 回执：当前材料绑定与单事务字节对照

工作分支 `codex/pure-linux-libg-headless-20261026`；基线
`49ccae96835da7a6db2b1aa6cf035bf9f7cc21fa`。交付 SHA 由包含本回执的提交确定，
避免提交内自引用。main 与冻结部署保持原样。

## 本阶段结果

- IMPLEMENTED / UNIT_PASS：只读材料捕获、Bionic局部重放、跨实验身份审计；
  空map XML由70bytes修复为Android实测65bytes。
- RUNTIME_OBSERVED_BOUND：当前材料与两次只读捕获、原Android事务及三个真实libg进程绑定。
- NATIVE_VERIFIED（限定事务）：一次受控store及原值恢复，双槽XML与进程身份恢复；
  两个Bionic进程的target/restore共8项字节一致。
- BLOCKED：Backup服务等价性、配置异常域与严格CATALOG_READY_PASS未通过。
  Bionic重放未加载libg；Backup显式未解析，write返回-1。
- NOT_RUN：ResourceManager、GameState、Step、完整局、训练和10×。

原始材料、XML、二进制及私有日志保存在隔离实验目录；公开仅源码、脱敏摘要与SHA。
没有追加真实Android写入、Backup或重启。

## 实际命令与退出码

材料捕获、受控事务和Bionic重放的输入/输出SHA见
[机器证据](pure-linux-runtime-preference-binding-evidence-20261028.json)。
报告列出实际NDK编译及guest linker64命令；编译、两次重放与六步VMTools均exit0。
三个真实libg进程native退出码为96/0/0、strict wrapper为1/1/1，候选消费者0/0。
其probe使用格式修复前版本；新probe只证明局部事务重放，未重新执行完整集成。
最外层run.exit为空的归档缺口保留，不解释为0。

```sh
python3 tools/pure_linux/runtime_preference_binding_audit.py \
  --capture "$PRIVATE_CAPTURE" --transaction "$PRIVATE_TRANSACTION" \
  --native "$PRIVATE_NATIVE" --replay "$PRIVATE_REPLAY" --output "$EXCLUSIVE_OUTPUT"
```

限定审计exit0，输出SHA
`dfc34c84488cd4e7c57154f1baee320c9a6d23f90e6cf87a074f6faaf21f4a77`。
环境为Apple Silicon macOS及已有Ubuntu aarch64 guest，kernel 6.8.0-138-generic；
输入为用户APK内匹配ARM64库，APK整包与历史锁定SHA的差异继续单列。

工程检查在独立仓库副本执行，使用已有Python3.13.5：

```sh
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py
/opt/anaconda3/bin/python3 tools/run_public_checks.py
```

依次951通过/1跳过、211通过、1104通过/42跳过，均exit0。
253份原XML的SHA逐项不变。首轮Python3.9缺cryptography及类型/slots兼容失败，
三组exit1与日志SHA保留于机器证据；随后重跑没有更改依赖配置。
源码清单2577项通过，gitleaks扫描42.88MB无泄露，diff检查通过，均exit0。
测试副本的10份本轮源码与最终仓库逐字节相同；最终提交记录对应交付版本。

## 首个未解决依赖与下一步

完整配置契约尚未闭合；优先建立Android Backup请求、去重、失败重排、
交付与原生候选持久化/所有权/异常传播之间的可验证对应。
本次空map单个ASCII事务不覆盖损坏密文、混合键、其他locale及中断恢复。
后续以独立实验缩小这些差距；严格catalog门前保持后续对象图NOT_RUN。

[完整报告](PURE_LINUX_RUNTIME_PREFERENCE_BINDING_2026-10-28.md)。
