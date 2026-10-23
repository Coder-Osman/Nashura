# 原生队列模式的只读限定审计

旧queue审计已核对manager→queue→ct86静态调用链，但运行时mode predicate未解。
本版本逐条绑定固定libg SHA的0x1062f6c..0x1062fd8共27条指令，
显式区分有活动world与无world的路径；只对活动world读数求值，
不推断manager+0x218无world路径的helper返回对象。

活动world类型为3时predicate=1；类型为1时读取world+0x1b0指针的+8字段，
其值6或15才为1；其它活动world类型为0。bit0-clear对应旧queue审计的age20分支。
这些是指令字段和分支结果，不擅自赋予游戏模式名称或语义错误名称。

NativeModeAudit复用冻结PRNG审计的PID/start_ticks、映射唯一性与暂停/恢复保护，
核对phase-snapshot probe及libg SHA；仅O_RDONLY打开所属引擎内存，
校验generation/epoch/tick、worker空闲、manager tick和world backlink。
不连接正在搜索的Full128引擎，不向其注入hook或修改内存。

独立原生任务复用另一诊断的冻结history-v2源码但不修改其目录，专用29773端口，
合法WAIT轨迹在90/1200/3600/5400 tick取样，检查原生digest不变、
过期身份拒收、快照恢复和两次续演一致；引擎由独立worker负责清理。
collector须外部launch SHA且worker结束后归档；verifier须外部launch/receipt SHA，
重核源码、载荷、运行时身份、逐阶段谓词及清理。

本证据只证明所采样WAIT轨迹的运行时mode字段，不证明全部实战分支、
mask等价、合法拒绝/pending可达性或隐藏随机机制全覆盖。
training_admissible=false、gates_closed=false，不替代G0/G1整体验收。
