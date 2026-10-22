# 拒绝历史 v2：独立工程版本

本版本用于补齐G0/G1中的拒绝证据契约，保持training_admissible=false。
没有替换主项目运行时，没有变更正在运行的Full128任务，也没有启动G2或正式训练。

## 来源与隔离

project/native_runner来自主项目460个Python文件的独立副本；
base-source-manifest.json记录复制前身份。project/tools的86个Python副本和
native_runner/data的12个规则资源分别由tooling-source-manifest.json、
resource-source-manifest.json记录来源与SHA。规则资源仍接受运行时原有摘要校验。
version-manifest.json记录本版本实际文件；verification.json记录测试与主项目冻结核对。
另补入5个C++测试资源，逐字节绑定主项目来源；这些不是新增运行时改动。
运行测试时PYTHONPATH只指向本版本project，避免导入旧运行时。

## 可见历史与拒绝语义

FairHistory采用fair-final-visible-history.v2，增加OwnRejection(tick, decision_tick)。
仅记录己方可见、private_to严格属于己方、对应既有ACT卡牌且准确到期的拒绝。
原生action ID、诊断原因、真实比赛seed及对手私有队列不进入该契约。
v1载荷不静默升级，额外字段拒收；sampler身份绑定v2历史与拒绝语义。

到期拒绝前若有新己方决策或公开执行，原预约已经被抢占，该拒绝不成立。
同tick对手公开执行与己方到期拒绝允许共存；己方执行与拒绝冲突。
逐tick记录拒绝，去重已有窗口事件；未记录的迟到事件、未来事件及矛盾事件报错，
失败时不部分写入历史。新决策与旧拒绝同tick时仍关联此前的ACT。

独立世界重演须同时匹配己方clock拒绝与最小可见证据。合法证据与目标历史不同
视为IncompatibleHistory，允许现有有界重建；缺失或格式损坏的原生拒绝证明是
协议错误，直接传播，不用世界重试掩盖。失败恢复上一根快照、历史与采样游标。
独立世界自行产生的对手私有拒绝不复制到己方历史，不再被一律判失败。

FinalBattleEnv中reserved card缺失分支在推进一tick后补发己方私有拒绝事件，
与已有到期判定对应。测试对该包装分支使用受控stub，不称为原生合法可达证明。
真实采集局遇到拒绝仍沿用整局隔离；本版本未放宽episode或recovery接收规则。

## 已验证范围与仍开放的缺口

rejection-tests.xml覆盖最小投影、时序/身份负例、同tick、窗口去重、原子失败、
冷启动与增量己方拒绝重演、缺证据不重试、替换快照失败回滚及对手pending保留。
所有这些是内存fixture与包装分支测试，不是原生整局证据。

此前复现的限制是：对手到期前存在的pending可在增量重演中自行拒绝，
但从开局重建时，隐藏计划采样只支持WAIT或较晚到期ACT，排除了提前私下拒绝。
本轮以frozen-joint-eighth-full-support-guided-replay.v1修补该工程支持集：
每个对手历史边界使用独立、可重放的RNG，以1/8概率从冻结完整合法策略采样；
其余7/8优先使用可直接重建的下一公开执行，否则沿用截止时间的backward log masses。
若截止条件概率为零，退回同一冻结策略的完整采样，再由独立世界推进检验历史。
未包装的基础provider也保留公开引导与完整策略分支，不预先排除提前到期ACT。

到期动作是否执行、私下拒绝或被更早公开事件抢占，由独立假想世界运行时判定，
不能把到期恰逢公开tick当作抢占。缺少直接预约映射也不立即排除世界，因为中间
可能私下拒绝、重新获得决策机会，再预约后续公开执行。整个公开轨迹、己方
拒绝证据、FAIR帧及根合法性仍逐项匹配；不匹配仅走原有有界world重建，最多32次，
默认4次。删除原先单边界128次预裁剪重试，不新增无界循环。截止检查贯穿候选
映射、推理前后、partition与sample，失败恢复上根及条件采样profile。

这是带引导的近似proposal与历史拒收，不是精确贝叶斯后验，也不保证有限预算
找得到全部兼容路径。1/8为本工程版本的显式参数，尚未通过总体校准或原生验收；
sampler身份连同base/prior/refill的配置哈希全部变更，不与运行中旧版本混用。
proposal-tests.xml验证内存世界冷/增量私下拒绝、拒绝后新预约再公开执行、
同tick到期交给重演、公开引导保留完整分支、零条件质量、罕见WAIT、截止时间、
非法策略动作及失败回滚。它不证明固定速猪下该拒绝在原生环境合法可达。

进入原生一般pending/rejection验收前，仍需固定速猪下合法拒绝的可达依据，
以及覆盖该支持集的明确采样方案、原生独立重演、Full128和重复快照恢复证据。
旧single-ready非法ACT注入诊断不代替这些要求。

## 运行

使用主项目.venv-live/bin/python，在工作区根目录设置
PYTHONPATH为本目录project的绝对路径，运行project/native_runner/tests下的测试。
regression-tests.xml中的统计与rejection-tests.xml有包含关系，分别报告，不相加。
主项目244个运行中冻结文件须保持与原attempt的launch.json一致。

## 本次收尾结果

拒绝专项21项通过；32个test_fair_final*.py文件的最终回归633项通过、
2项跳过，零失败/错误，78.39秒，报告为regression-tests-complete.xml。
两项跳过均因独立副本未安装冻结libg.so，涉及随机对象与技能对象静态证据；
它们未被本轮重新验证。此前regression-tests.xml保留缺C++资源导致的
1失败/632通过/2跳过记录；补齐来源一致的资源后重跑，不删除失败证据。
专项与最终回归有包含关系，不能相加；也不与34个限定PASS子项相加。

上述21/633报告属于修补前身份，原manifest与verification另存为
version-manifest-before-full-support.json和verification-before-full-support.json。
它们不替代本轮proposal-tests.xml与regression-full-support.xml的新源码测试。

verify_version.py核验564个版本文件、6个生产文件及2个既有测试文件变更、5个C++资源来源、
独立import路径及旧attempt的244文件零变化，并生成版本清单与验证报告。
使用主项目.venv-live/bin/python运行本目录verify_version.py可重复核验。
结果仅为工程检查通过且原生范围明确排除；native=false、deployed=false、
general_pending_closed=false、training_admissible=false。G0/G1不因此闭合。

## 完整策略proposal修补验证

本轮三组专项88项通过，1.0秒；32文件完整回归647项通过、2项跳过、
零失败/错误，78.59秒。跳过项仍是缺冻结libg.so的两个静态原生检查。
两组报告有包含关系，不与旧21/633报告、34限定子项或整体门数量相加。
固定速猪下拒绝的原生合法可达依据、新版本独立重演/Full128/恢复、
原运行版本完整终局及总体先验校准均未在本轮完成。
