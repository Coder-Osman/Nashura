# terminal选优超时后的独立有界恢复

本入口仅处理已停止、已归档且保留非terminal完整checkpoint的原生选优超时。
父失败保留；独立attempt复制冻结部署和输入，从同一模型、AdamW、RNG、局内
采样游标及选优历史继续。预算1800–86400秒，不修改父运行时的预算或状态。

## 输入、执行和归档

`verify_parent.py`要求外部固定父launch及归档回执SHA，重验失败原因、源码、
数据身份、原始第7步与超时checkpoint的latest/best完整游标及历史前缀。只重验
已有数据内容，不声称重新核查原生对局来源，也不签发live resume通过。

`run_retry.py prepare`先运行上述核验，独立私有目录绑定request、worker、冻结
运行时、父输入清单和验收器SHA；`launch`严格SSH主机身份及上传字节核验，
先保存launch intent以防连接不确定时重复启动。运行前核对父PID/start已退出，
取得GPU UUID租约并沿用原资源预检，实际子进程显式使用超时checkpoint SHA。
输入及祖先符号链接拒收；只复制inputs、deployment和恢复checkpoint，不继承
父失败状态为新结果。超时或异常隔离，所属子进程组按原helper清理。

`collect`在worker、训练子进程或GPU租约仍活跃时不封存。停止后逐文件SHA及
字节数核对，下载前后重新读取远端清单；重复收取保持原receipt字节，差异拒收。
同SHA大文件可复用父本地副本，原始父归档不写入。

## 独立验收与范围

`verify_retry.py`重新绑定父内容、request、归档、进程、执行参数和冻结部署，
重建数据身份；复用原terminal内容验收，额外要求超时历史前缀保持、更新步数
实际增加，继承best时其完整trainer状态一致。无terminal或失败归档拒收。
工具、小模型和真实子进程fixture不等于完整原生terminal恢复通过。

所有产物固定`training_admissible=false`、`formal_g2_pass=false`、
`gates_closed=false`。此工程不自动启动round2、正式1280局或五机放量。
权重、私有数据、训练行身份及远端原始审计保留在嵌套私有目录，不公开导出。
