# 开局先验诊断工程与原生启动记录

2026-10-23基于主线18e984c，新增独立开局采样、统计及归档验收入口。
固定1024个seed及21个事前检验，第二个新原生进程复测前64seed；
详见CONTRACT.md。测试覆盖完整排列、seed/顺序/重复、统计重算、归档身份、
清理记录及篡改拒收，34项通过、1.325秒，见tests-final.xml。
测试使用synthetic fixture，不作原生统计结论。

首attempt native-openings-1792729974888937000因远端缺SciPy，在引擎启动前失败；
原始回执SHA为310854bc4dbbc142ac42d70c81de2097e70bf6f34c079e6c379076bd52c43849。
统计依赖改为本机诊断时惰性导入，原生采集不依赖SciPy；独立新attempt
native-openings-1792730118175394000已在.35、专属29777端口启动。
launch SHA为6239debaf3ec71281135e4f15ce14e0b676524840ffb0015ccdc368f41edec10。
本次记录时primary至少81/1024、repeat尚未开始，仍运行中，不提交分布PASS。

probe SHA为49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba；
采集运行时逐文件冻结。此实验未另提供libg映射观测，不推广为全部隐藏RNG覆盖。
完成后须外部固定下载回执，重验全部行、种子、21个统计量、复测及所属引擎清理。
未拒绝零假设仅是有限诊断，不证明40320排列全可达或精确均匀，不证明历史条件后验。
training_admissible=false，gates_closed=false；G0/G1与34/43登记保持开放。

## 原生收尾与独立验收

上述attempt已完整结束：primary 1024/1024，7947.972344秒；第二新原生进程
repeat 64/64，504.683079秒。17份归档的完整文件集合、字节、冻结来源、预注册
seed顺序、八牌排列、行数、两个不同进程及所属引擎/端口清理均通过独立验收。
64个seed的双方可观察开局牌序跨进程完全一致，不扩展为隐藏RNG状态一致。

事前21个检验均未拒绝；最小p值0.07721489409，高于Bonferroni阈值
0.00047619047619。双方各观察到1006/1016种不同完整排列。结论仅为有限样本
及所列统计量未发现偏离，不证明精确均匀、全部40320排列支持或历史条件后验。

- 外部固定launch SHA：6239debaf3ec71281135e4f15ce14e0b676524840ffb0015ccdc368f41edec10。
- 下载回执SHA：ff248afd4057235581db567fd05300551eba93d4a338f0a6768d9fdee9363fd0。
- 独立验收SHA：9b16fbb6acf2cd56857eb98bceb61b8cf20b611c667243d8f6d0f7eacf645534。
- status=verified_diagnostic；公开native-evidence-summary.json保存完整统计和身份，
  原始seed/牌序/进程审计留在私有attempt，缺私有附件时不声称可重验全部原生内容。

本次未修改采样契约、运行源码或其它冻结任务；不把34项工程测试与原生采样
计数相加。training_admissible=false，gates_closed=false，G0/G1与34/43不变。
