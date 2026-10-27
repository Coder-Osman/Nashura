# Codex回执：E2.1-N原生初始化

分支：`codex/pure-linux-libg-headless-20261026`。
基线提交：`2624b86f7b025d56c4abeb908996c7824448d1aa`。
本轮实现与实验记录提交：`cb8b10bdd24dd0de778124a260aeced33c3f94a9`。
本回执的后续提交仅补齐该提交标识及对应发布清单，不改写实现或实验历史。

## 实际结果

| 子项 | 状态 | 真实证据 |
|---|---|---|
| 原版libg加载 | LOAD_ONLY_PASS | NOW/LOCAL、dlclose、exit0，187输入SHA前后相同 |
| constructors/映射 | CONSTRUCTOR_TRACE_PASS | libg753进入/753返回；全体1070/1070 |
| content root/context | CONTENT_CONTEXT_PASS | 原生构造exit0，root/context可读、ready=0 |
| resource manager/catalog | BLOCKED | filesystem分配成功，provider/表加载尚未闭合 |
| battle manager、GameState各节点 | NOT_RUN | 前置catalog未就绪 |
| empty battle/reset/GameStateStep | NOT_RUN | 没有推进tick，不将Tick getter当Step |

post-load限定27地址观测通过：两正对照各1，其他已知JNI/Asset地址零命中。
constructor内部和其他JNI/间接路径未覆盖，零计数不等于依赖不存在。
实际系统调用含Android属性和logdw路径尝试，不声称Android-free。

## 命令、身份与输出

实际编译、runner、gdb/strace命令及其逐项退出码见
[报告](PURE_LINUX_NATIVE_INIT_2026-10-27.md)和
[JSON](pure-linux-native-init-evidence-20261027.json)。环境为真实Ubuntu22.04.5/aarch64，
输入libg SHA `110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
APK整包历史SHA差异保留，8个游戏库、assets和runtime/update沿用独立身份。
实际加载模块及其完整SHA、constructor/动态观测/系统调用日志SHA均在JSON。

原生child exit：baseline/debug/strace/v4/v5/v7均0；v3为23；v6 inferior32而GDB0。
v6的零计数无效；v7修复断点与guard冲突后，真实inferior0且正对照通过。
REA限定父函数调查超时/取消、初次写满磁盘及完整归档恢复记录全部保留。

## 工程检查

- `python3 -m unittest discover -s tools/pure_linux -p 'test_*.py'`：exit0，42项通过。
- `/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py`：exit0，211项通过。
- `/opt/anaconda3/bin/python3 tools/run_public_checks.py`：exit0，9组共1104项通过、42项跳过；其中主组794通过、27跳过。
- `python3 tools/check_public_snapshot.py`：exit0，2364份源码/报告SHA核验通过。
- `gitleaks dir . --config .gitleaks.toml --redact`、`git diff --check`：均exit0，扫描无泄漏。
- NDK27.3 AArch64 Android24探针`-Wall -Wextra -Werror`最终重编译exit0；binary SHA见JSON，实际guest所用probe身份另列。
- 大回归首次使用系统Python3.9在收集阶段失败：两个wrapper exit1，pytest exit2；8/20个collection errors及原始日志保留。随后使用已有Python3.13.5通过，未安装或覆盖系统组件。
- 回归在独立源码副本运行，XML及日志SHA见JSON；历史公开测试文件未覆盖。
- 工程测试不替代catalog、原生生命周期、无VM或完整战斗验证。

## 未解决项与下一步

唯一当前前置阻塞：**provider注册参数与content service/table加载顺序没有闭合，catalog ready=0**。
尚未证明JavaVM必需，也没有证明其可以从后续对象图移除。
下一步先核验真实资源路径/string对象及初始化调用点，以新子进程逐项推进资源注册和表加载。
自然ready后才进入battle manager、GameState、empty/reset及真实Step。

main、origin/main及冻结部署保持原样。原始输入、资源、内存和私有日志不上传；
无正式训练、无多机放量、无10×实验。
