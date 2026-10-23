# 手牌临时随机状态与初始化引用审计

本版本补充 G0/G1 隐藏随机机制清单，独立于正在运行的 Full128 和迁移部署。
仅执行本机静态读取，不启动或修改引擎，不把静态引用等同于实际可达性。

`shuffle_audit.py` 首先核对固定 libg 的完整 SHA256，再核对 14 段完整字节摘要、
关键调用参数与分派指令。扫描所有可执行 PT_LOAD 的直接 B/BL，核对 18 处
MT 初始化引用及各自唯一的 EH 函数范围。没有把 18 处都解释为战斗中实际使用。

手牌路径为 wrapper → hand_initialize → hand_shuffle。在所审计分支中，
seed 是世界 manager 随机输出与玩家 identity 低字的 uint32 和，临时生成器位于
栈 sp+0x40，两处 generate 与 clear 都传同一地址。控制流从初始化返回之后
遍历 225 个指令/清理状态组合；所有可达正常返回前都调用 clear。分析将直接调用
视为正常返回，排除异常、进程终止及非局部跳转；不作调用图全局证明。
这说明本分支的临时对象在正常函数边界结束前释放，而非永久挂在比赛对象中。
快照是否足够还取决于上游 manager 状态、初始化产物及其它持久随机对象。

另将旧 inventory 中 tick-seeded MT 路径绑定到 ct1548/subtype55：核对 factory
uint16 表项、构造器 vtable 写入、执行器与 type getter 的 RELATIVE 重定位及
subtype 分派表。它与手牌路径不同；名称和 command type 不构成固定速猪不可达证明。

本版保留以下未闭合项：另外 16 处初始化引用的语义/可达性，间接分派，所有
seed 来源与其它随机算法，真实手牌路径观测，排列分布与经验先验校准，以及
全部可达随机机制的 FAIR 非干扰。hand_initialize 还有规则相关替代分支，
不声称均匀全排列、精确后验或所有模式都走此路径。

复核命令（私有游戏库由使用者另行准备）：

```sh
PYTHONPATH="_spec_work/versions/fair-final-history-v2/project" python \
  "_spec_work/versions/fair-final-native-shuffle-audit-v1/audit_shuffle.py" \
  --library "PATH_TO_MATCHING_LIBG" --output "REPORT_PATH"
PYTHONPATH="_spec_work/versions/fair-final-history-v2/project" python -m pytest -q \
  "_spec_work/versions/fair-final-native-shuffle-audit-v1/test_shuffle.py"
```

公开版本只有源码和摘要，无第三方库；缺私有库时原生静态重算及六种元数据/
代码篡改测试跳过，其余分析器测试照常运行。所有准入/总门字段保持 false。
