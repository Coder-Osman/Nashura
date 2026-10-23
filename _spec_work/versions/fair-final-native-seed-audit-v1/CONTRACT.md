# 原生种子生命周期的限定验证

目标是补充G0/G1的随机状态覆盖证据，区分固定libg中种子初始化的静态语义、
实际进程的时间缓存、原生快照续演和跨进程重复初始化。不是完整随机机制清单。

`seed_audit.py`绑定libg与phase probe的SHA。静态核验initialize、seed、constructor、
time PLT四个完整函数区间，以及DT_INIT_ARRAY中的构造器重定位和time导入。
语义是缓存非零时复用缓存；零时调用time；uint64(cache-base)>15则uint32 seed加一，
保留无符号下溢与回绕。它不等于每次设置seed都读取当前时间。

原生探针只使用独立端口29775及自己的引擎进程。只读/proc内存前核验PID、启动
时间、二进制映射、worker空闲、generation/epoch/tick和manager tick。读数不进入
策略输入，不连接或修改后台Full128部署。合法WAIT轨迹在90/1200/3600/5400 tick
检查digest不变、过期身份拒收、快照恢复、两次续演一致和句柄释放。

同一进程重复reset前先关闭旧连接，再创建新的compact transport与环境，允许
reset执行90 tick warmup，之后才开启单tick保护。该操作不重启引擎，不私改旧
transport的内部保护状态。失败时关闭新连接，worker最终清理自己的引擎和端口。
首个旧探针在复用atomic transport进行warmup时失败，原归档保持，不登记通过。

独立两进程分别立即与延迟到base+16秒之后开始首局。记录缓存是否在首局前已非零，
不假设延迟必然使种子加一。比较两进程同seed的初始与四阶段digest及manager RNG，
同时验证同进程同配置重复reset；任何差异或超时保留并停止，不跳过失败。

回收要求外部固定launch SHA且worker结束；验收要求外部固定launch/receipt SHA，
重核上传脚本、静态摘要、冻结runtime、原始归档、两份探针报告及引擎清理。
原始私有归档不发布，公开摘要绑定其SHA与范围。fixture和篡改测试只验证工具。

即使限定原生检查通过，仍不证明所有全局读写、战斗组件随机性、实际策略搜索
覆盖或快照序列化了全部全局。`training_admissible=false`、`gates_closed=false`，
保持G0/G1整体开放，不启动G2–G8或五机放量。
