# 按用户文档执行：P0 / E0

依据：`/Users/gujiga/Downloads/CRAI_search_student_distillation_diagnosis_v1.md`。

## 指令优先级与边界

- 用户明确要求先停止旧实验，因此已停止旧温度对照PID165744，保留checkpoint和日志。文档建议先跑完E1/E2不覆盖这次明确停机指令。
- 用户后续要求停止五机512局教师采集，现已停止并保留124局完成回执。释放的算力优先用于 `../teacher-sampling-eval-20261013/README.md` 的教师访问分布采样实战。P0审计、E0评测保留独立目录、端口、权重和GPU进程。
- 文档按P0→P1→条件触发的DAgger/网络修改逐级推进，不提前改网络、价值头或PUCT。
- 原协议保留正常取消的合法计划监督。文档“取消决策不参与有效训练时钟”的说法与此前协议不完全一致，本次不悄悄更改；旧数据缺少完整执行回执，标为execution_status=unknown，不宣称已执行，不以它统计实际出牌成功。
- 当前总体及WAIT/ACT指标使用整局等权，各子集分母不同，文档通过三个总体均值反推37.5% WAIT占比的计算前提不满足。P0重新输出实际分子、分母和整局bootstrap区间。

## P0-A 联合解码

- 从原始128局逐局取4个状态，按玩家、时间段、旧教师WAIT/ACT分层，总计512状态供重搜索；它是分层诊断样本，不代表自然频率。
- 在原16验证局所有决策上评估初始模型与已完成的balanced学生。
- 学生权重固定为上一轮balanced/best.pt，加载前核验SHA。
- 比较当前逐级贪心与width32前缀beam；WAIT独立保留，评分使用原始全合法空间联合概率。
- 网络代码已核对：位置条件依赖卡牌，延迟条件依赖卡牌和位置；不需要先重构条件动作头。
- 初步离线参照明确标记original-exploratory-visits，不冒充clean教师；clean结果另存后再对照。
- 输出ACT完整动作、双方ACT时卡牌、正确卡牌时位置、正确卡牌与位置时延迟等条件指标；排名只在beam与旧候选并集中计算，明确不是全空间精确排名。
- 时间段仅用0–60/60–180/180秒以后，不冒充实际单双三倍圣水；攻防、超时等待等缺少可靠标签时标记未提供。
- information往返序列化摘要、合法掩码摘要、模型身份、合法性校验逐帧执行。

## P0-B 干净教师重搜索

- 输入只有允许的InformationState，不加载真实比赛私有seed/对手手牌/隐藏计划。
- 每状态独立重建2个兼容世界，两次分别检验clean mode稳定性。
- 每个世界内三个搜索共享同一可恢复根：clean32、noisy32、clean128。每次搜索固定同一seed，保存候选先验、有效先验、访问、Q及总回传值；根噪声仍会改变后续搜索随机序列，因此比较的是整体两种搜索方式，不声称完全隔离了每一个随机变量。
- 保存根信息SHA、标签mask SHA、模型SHA、搜索配置、独立世界seed及轨迹摘要。
- 正式SearchConfig和训练数据仍要求32访问；128只在显式diagnostic_simulations入口使用，不进入正式SearchEpisode。
- 额外Q后悔值使用同一世界内4个共同随机种子的冻结策略续局，最多6事件/10秒；这是近似叶价值诊断，不是完整终局真实Q，也不是每动作独立完整MCTS。
- 先验收1个根，成功后开启4个审计worker。失败时保留证据，不用真实完整状态替代。
- 正式审计端口30680–30683；早期33880、35880落在Linux临时端口区32768–60999内发生占用，失败记录保留，已切到区间外。

## E0 实战

- 不训练，使用同一个balanced学生policy.pt。
- baseline为原始教师策略直出，SHA d824335a3c5757d9de4dad2d9b31a3d619d754a2f046ecaa6cd55a5ae1566f07。
- greedy与joint-beam各128局，共256局；两组相同64个比赛种子27614000–27614063，各自换边。
- 已核对种子不与已有采集/价值训练/已知评测重叠。
- `.35`4路，端口30780–30783，独立目录`/home/zhichun/fair-distillation-e0-20261013`；后台自动汇总配对置信区间。
- Python beam与离线审核使用同一函数；ONNX未在本实验使用，不宣称完成ONNX等价验证。
- 不自动晋级。128局每组是诊断，不替代正式400/2000晋级门槛。

## 测试与后续门槛

- 原搜索与新解码组合79项测试通过；随后新增完美拟合分布的逐级greedy反例及部署adapter测试，新解码6项全部通过。
- 可枚举动作全集概率和为1、beam等于精确MAP、WAIT-only、文档反例、部署adapter评分一致且权重不变、诊断128预算与正式32隔离均通过。
- clean标签充分且语义审计通过后，做100–1000状态小集合拟合；再按同划分同预算安排E3/E4。
- ACT条件loss、alpha/lambda消融、DAgger与网络修改尚未启动，不把前置诊断当成整份文档已完成。

## 文件与状态

- 本地manage.py：deploy/decode/smoke/search/status。
- P0远端：`/home/zhichun/fair-distillation-document-p0-20261013`。
- audit/contract.json、roots.json、roots/*.pt、episodes/*.json、report.json记录离线结果。
- clean-*/labels/*.json保存重搜索标签及证据。
- launch_e0.py启动E0，e0_supervisor.py执行并汇总。
- after_smoke.py为本次有限实验的顺序启动器：首根完整成功并且所属引擎清理后才启动4个clean分片；首根失败或超过1小时则记录needs-inspection，不扩容、不替换信息来源。
