# E2.1 语义矩阵：证据范围

机器可读结果为 [e2_1_semantics_matrix.json](e2_1_semantics_matrix.json)。
`verified` 只适用于下表的窄范围；所有 PN 路径仍经过原 JNI。

| 项目 | original | shadow / replacement / strict | 当前判定 |
|---|---|---|---|
| 固定完整轨迹、快照、28动作、restore/reset | 全字节对照 | 32键替换对照一致 | verified，非全配置语义 |
| 正常PN、重复读、两次reset读 | 实际桥调用 | PN均原fallback | 原桥观测通过，未替换 |
| 空/malformed/UTF-8/0与4095bytes | 实际桥写读一致并恢复 | PN均原fallback | verified，4095为探针界限 |
| 空值后的fresh-process自然读取/写回 | 原引擎入口 | 未实现native持久化 | original verified |
| malformed后的fresh-process自然读取/写回 | 原引擎入口 | 未实现native持久化 | original verified |
| 缺键后的fresh-process自然读取/写回 | exists=false、读空、自然写回 | 未实现native持久化 | original verified |
| 顶层临时key exists/write/read/remove | 原生顶层及raw桥 | 未新增replacement | probe-induced verified |
| 正常JNI ExceptionCheck | 后期probe线程前后false | strict同范围 | 无异常产生，非异常清理覆盖 |
| 七个自有原生字符串析构 | 真实ctor/dtor/libc.free | strict独立组件probe | verified，仅自有对象 |
| 未知键拒收 | 非strict显式缺键probe原桥返回空 | strict真实abort exit1 | verified negative |
| 真正重复配置初始化 | 未执行 | 未执行 | unknown |
| commit/apply时机和持久化完成确认 | 未定位 | 未实现 | unknown |
| 并发读写/可见性 | 未执行受控交错 | 未实现等价契约 | unknown |
| 只读写入/未初始化/真正超界值 | 未闭合 | 未闭合 | uncovered |
| 异常触发/清除/失败清理 | 未触发 | 未闭合 | uncovered |
| 配置close/free/释放后错误 | 未闭合 | 未闭合 | unknown |
| Java local/global引用退休 | 未闭合 | 未闭合 | unknown |
| MUTF-8/数组/对象返回 | 未闭合 | 未闭合 | uncovered |
| 初始化对象大小/对齐/布局/所有权 | 仅入口与顺序 | 未建立完整替代链 | unknown |

完整轨迹一致不消除上表空白。所有46排除键仍required，G-E2.1 exit2，
G-E2.2/G-E2.3及Android-free/训练准入均未通过。
