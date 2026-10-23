# 最新实现独立原生验证：发现数值回归，修复后重新验证

## 2026-10-23 远程选优派发与回收工程验收

基于 Nashura 主线 `5c54e0478747d30be2472f855c06a871d8866406`，新增
`selection_remote_runtime.py` 与 `selection_ssh.py`。运行时要求远端上传树与
`remote-config.json` 清单精确相等，上传文件均为普通文件并逐字节匹配；归档时重核
输入、部署、worker和运行时副本，回收前重读远端配置/进程/失败记录并交叉校验
SHA、字节数、JSON内容及本地固定副本。重复回收也重新验证元数据。

新增 `test_selection_remote.py` 覆盖GPU/内存/磁盘预检、GPU文件锁、进程启动身份、
原子SFTP、精确上传树、部署清单、元数据/worker篡改拒收、活动进程不下载，以及全模拟
SSH launch编排。专项 **29个测试案例通过**；与既有selection handoff/execution/
active verifier联合 **93项通过，98.774秒**，集合重叠不相加。完整结果见
`tests-selection-remote-joint.xml`。该回归全程模拟SSH及远端文件系统，未连接GPU主机，
不代表真实父恢复、派发、terminal选优或G2/G3通过。

不触碰冻结部署；`training_admissible=false`、`formal_g2_pass=false`、
`gates_closed=false`；G0/G1仍开放，中心200 GiB资源门槛与原生Full128/父恢复待验状态不变。

代码基线为已整合审计分支的Nashura主线 `af498d14371a2ddb91b5b5794fa1a32b4064629a`。
2026-10-22重新fetch并核对全部远端分支，该提交仍为最新main。

独立载荷277文件，active adapter、learning、model、policy、search、fair_migration
六文件与当前源工程逐字节相同，trainer与原冻结父包一致。父载荷、deployment、
逐文件源码及新上传包分别绑定SHA；恢复身份额外包含六个active文件，拒绝跨实现恢复。
scalar参考保留旧adapter/learning，用独立模块导入，不将新KL作为自己的参考。

新增10项CPU工程测试通过（最近重跑32.96秒，见tests.xml）：源码覆盖与参考隔离、父包/
部署篡改、路径逃逸及重复成员、变换锚点失配、冻结/解冻阶段真实模型与优化器
恢复，并拒收变更后的参考SHA和训练指标。测试夹具仅证明工具行为，不替代CUDA。
首次恢复测试沿用patience=1导致小模型在第6步提前终止；测试夹具改为patience=2
后重跑通过，原生训练配置没有因该夹具调整。

首次尝试native-active-migration-1792675967128763000在183.464秒失败并自动停止；
完整失败归档287文件且部署身份一致，后续reference和两个恢复阶段没有运行。
冻结三对通过，解冻第一对AdamW更新超出既定容差：
encoder.relation_transformer.blocks.1.spatial_bias.2.bias第3项差4.107132554e-6，
两路梯度为-4.321106667e-11与-4.753217264e-10。损失和梯度比较通过，
但近零梯度的差异经AdamW放大；保留失败，不放宽atol=3e-6、rtol=2e-5。

修复learning.py：编码器解冻时，教师和学生delay均保留逐位置路径；
编码器冻结时保留合批。回归测试明确检查两侧batch调用，11项learning和
26项migration通过；这些CPU测试不替代CUDA。源码修复基于af498d1。

修复后独立尝试native-active-migration-1792676550169895000，.35物理GPU1，
PID50734/start26665117，最长6小时。快照1792676859：benchmark exit0，六对
损失、梯度、AdamW比较均通过，解冻三对最大梯度差为0，已进入reference。
单帧配对加速比中位数1.01174；冻结两个可ACT帧分别约6.14及6.54倍，解冻
保持scalar约1倍，WAIT帧约1倍。不将不同阶段时长中位数之比视作总体加速。
详见cuda-comparison-evidence-20261022.json；原始附件保持私有。
后续自动执行frozen恢复、trainable恢复，任一失败即停止，最终归档独立验收待完成。
没有启动正式数据采集或模型发布，旧失败部署和其它运行中任务保持原样。

原Full128搜索任务保持冻结部署：快照1792676578已提交92组/184根/23552模拟，
tick3510玩家0搜索中；尚未完整终局。
原batch-v1链路仍在selection，前四阶段正常结束；这些旧实现结果单独保留。

G0/G1仍开放，限定子项34/43保持；G2–G8未运行。新CUDA与跨进程恢复仍待完成
并独立核验归档，training_admissible=false、formal_g2_pass=false。

## 2026-10-22 最新主线确认与独立归档验收器

再次fetch全部远端分支，main与本地HEAD同为
`da8ca6c12653add5cc4a8667f4809db9625a0ac7`，审计分支8195a5a已包含于主线。
公开清单2000项源文件与源工作区逐项SHA一致，无旧源覆盖新版代码。

新增verify_active.py和test_active_verifier.py。验收器外部固定launch/回执SHA，
重验部署、四阶段独立进程与全部比较附件；从六个checkpoint重算状态、数据与实现
身份、采样顺序、预算、选优历史及encoder冻结/解冻。352/256/96与microbatch1
日程默认强制，fixture显式放宽时结果清楚标为非原生日程，不接受为原生恢复证据。
21项专项通过，60.40秒，见tests-verifier.xml；包括真实小模型七步连续训练与两个
恢复分支、重封装后的配置/采样篡改，以及归档失败/缺项/重复进程拒收。
随后载荷、恢复及独立验收器联合31项通过98.27秒，见tests-combined.xml；
包含上述21项，不累加。实现、测试及快照SHA见verifier-evidence-20261022.json。

快照1792678182.4514027：新active reference已完成首个256行冻结更新，
初始358行验证479.622秒；随后完整验证进行中。supervisor50734及reference50850
启动身份匹配、部署一致；尚无完整reference报告或两个新进程恢复结论。
快照1792678259.0740073：旧batch-v1前四阶段exit0，selection50195仍活跃，
尚无terminal选优结论；该旧实现结果单独保留。
快照1792678182.4546835：Full128仍提交93组/186根/23808有效模拟，tick3558
玩家0已完成128访问、玩家1搜索中；整组未提交，尚无原生终局。

本次完成的是独立验收工具，不是对未完成任务签发通过。原生恢复、完整搜索终局
与整体门继续待验；G0/G1开放、34/43登记保持、G2–G8未运行。

快照1792678687补充：active reference已完成256行及96行两次更新，尚在reference；
Full128已提交94组/188根/24064有效模拟，tick3592搜索中、未终局；旧batch-v1
仍在selection。三个原始快照的路径、字节数和SHA固定于上述verifier-evidence摘要，
原始附件独立私存。中心磁盘137.454GiB，正式200GiB门槛保持。
## 2026-10-23 独立解冻恢复重试与后续交接

父 native-active-migration-1792676550169895000 的 benchmark/reference/frozen
均 exit0，trainable 共享总预算耗尽 exit124；父仍保留为失败，未登记恢复整链通过。
新增 retry_active.py、retry_runtime.py、verify_active_retry.py 和专项测试，外部
固定父归档身份，独立预算只恢复 trainable 第5→7步，并核验原始冻结输入继承。
恢复/验收联合46项工程测试通过54.57秒，见 tests-active-retry.xml；首轮导入错误
及修复记录保留为 tests-active-retry-initial.xml，均不替代原生恢复结论。

独立 native-active-retry-1792720054258999000 已在 .35 物理GPU1启动：
supervisor54188/start31014987，恢复54191/start31015203，预算28800秒。
复制287份冻结输入共683466574字节；原实现、数据、2轮冻结/解冻日程、batch256/
microbatch1/尾批96、确定性环境均保持。最新回收检查仍为 running，尚无新恢复
独立内容验收，也未启动 terminal 选优。沿用通用子进程 helper 的 status.phase
显示 selection，但实际命令为 trainable 恢复，不把该字段当作选优已运行。

同时补齐 prepare_selection.py / run_selection.py 的显式重试链交接，保留旧成功父
要求，失败父单独进入仍拒收。新测试包括实际子进程恢复参数贯通、缺失/篡改链、
身份复用及门升级拒收；小模型夹具不满足真实352/358/334日程，执行入口继续拒收。
首轮测试夹具漏导入 comparison，57项通过、11项setup错误；随后补齐显式导入，
并将夹具断言与真实日程准入分开，历史失败XML原样保留，不调低原生门槛。
第二轮140项通过、1项因小夹具被真实日程门拒收而断言失败，保留为
tests-retry-selection.xml。修正断言后，恢复/交接/执行/SSH原型/内容验收联合
141项通过182.68秒，见tests-retry-selection-final.xml，集合重叠不与46项相加。
源码、测试与原生启动身份见active-retry-evidence-20261023.json。真实重试仍待完成。

Full128 保持后台运行。快照1792720941.0022864：已提交107组/214根/27392有效
模拟至tick4038，worker及29761/29762双引擎在线、无失败、尚未完整终局。
可用内存238.851GiB、磁盘206.292GiB；瞬时容量不替代两代正式容量验收。
本轮未补齐随机状态全覆盖：间接分派、临时seed及未证实的组件仍待证据，不新增
随机门PASS。G0/G1开放、限定登记34/43，G2–G8未运行，不五机放量。
