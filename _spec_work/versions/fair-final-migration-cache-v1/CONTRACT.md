# 迁移整局解码校验缓存：独立候选

## 2026-10-22 确定性探针超时与独立有界重跑

native-deterministic-round0-1792641532086311000遵守原5400秒上限退出124，
实际5400.666秒；日志只有首次更新开始，未生成更新完成报告。父及配套恢复
各278个归档文件SHA/字节数核验一致，原冻结部署保持。独立读取父已保存
round0.pt，latest optimizer_steps=0、history仅初始验证、terminal=null，
明确不作为成功更新。旧恢复在父失败后自行退出，未启动reference/resumed。

启动器新增显式--timeout-seconds及--phase-timeout-seconds，旧默认5400/
10800秒不变；分别限制1–21600和1–43200秒，非法值在读取父或SSH前拒收。
supervisor仍有TERM/KILL收尾，恢复等待仍由父预算加300秒派生，预算写入
冻结launch/settings。11项测试通过5.084秒，包括原确定性运行时/载荷身份
专项，范围与此前测试重叠不相加；编译及diff空白检查通过。

独立重跑native-deterministic-round0-1792647014623244000，预算14400秒；
271个训练源码SHA与旧确定性运行逐项相同，三局数据、batch256、microbatch1、
FP32、种子、单更新及严格逐位恢复校验保持。新父supervisor47472/
start23710962，训练47474/start23710967。配套
native-cached-resume-1792647047730238000，worker47579/start23714129，
等待14700秒，各对照阶段28800秒。快照1792647089：父正在索引，恢复等待，
进程启动身份匹配、冻结部署一致。未修改旧运行预算或权重，不宣称新更新/
恢复通过。实现、旧失败归档及新部署身份由bounded-rerun-evidence-20261022.json
固定，training_admissible与formal_g2_pass保持false。

## 2026-10-22 确定性恢复修补

最新快照1792644539：严格确定性父完成358/358帧初始完整验证，2705.650秒，
已进入第1次更新；round0.pt已出现，但尚无更新/轮次完成凭据。训练46541/
start23162609与supervisor46539身份匹配，冻结源码一致；配套恢复46619仍
等待父正常结束，尚无新恢复PASS。后文96/358等是较早快照，逐位比较保持。

更新快照1792642554：普通全量诊断已完成，exit0、1449.279秒；全部358行
重验证loss差-7.408957269916527e-8、policy KL差-6.243503269942607e-8、
WDL KL差-4.703439082653915e-9，exact_validation_match=false。
原/恢复网络语义SHA相同，上传的诊断源码SHA核验通过，完整report/log/completion
已收取；不将诊断正常退出当恢复通过。下文“诊断仍在运行”为较早快照。
新严格确定性父验证已到96/358行，配套恢复等待父，两者冻结载荷一致；
新CUDA单步与恢复结论继续待验，逐位校验保持。

旧缓存单步探针native-cached-round0-1792632378773664000已成功完成，
2450.776秒，初始358行验证1144.998秒，optimizer_steps=1；checkpoint SHA为
094f6cddd2056f1b1952cf7afd065485a77a61de8469d2a53cff374b49b6f812。
配套native-cached-resume-1792632497065870000在重新验证best时失败，
错误为selected checkpoint live validation differs from stored metrics。
父及恢复部署分别277/283文件完整归档核验；未放宽校验或改写旧checkpoint。

独立只读诊断native-resume-diagnostic-1792640951915657000在相同模型、
同一帧和eval/no_grad下重复计算，已观察到loss、policy KL及WDL KL微小差异；
全358行诊断仍在运行。对照native-resume-diagnostic-1792641140978160000
启用严格确定性算法、进程内CUBLAS workspace及关闭TF32后，三个代表帧的
重建/恢复/恢复重复指标逐位一致，网络语义SHA均为
1cf7d9399d537db8ebb7dc91d9f5aa45930ea656cc5640afc30af840955d4d04。
该对照exit0、191.166秒；仅是三帧前向证据，不代替梯度或新进程恢复。

build_payload的显式deterministic选项生成独立载荷：新deterministic_runtime.py
在教师加载前配置训练及恢复入口，确定性算子采用严格报错模式；设置只属于该
Python进程。运行时模块纳入series/selection实现SHA，原逐位验证保持。
恢复入口取父载荷内固定SHA的probe，不以本地未绑定版本覆盖。默认旧构建行为
保留，旧运行中部署不变。独立执行验收器同时覆盖新运行时模块身份。

tests-deterministic-runtime.xml：19通过、47.44秒，范围包括缓存、完整梯度/
AdamW对照、采样和选优恢复、执行归档篡改拒收、进程内确定性开关、晚配置拒绝、
新模块身份绑定。此范围与旧15项重叠，不相加；本地测试不代替CUDA恢复实证。

新父native-deterministic-round0-1792641532086311000，supervisor46539/
start23162603，仍是三局工程数据、FP32、batch256、microbatch1、单更新，
上限5400秒，从新模型/AdamW开始。快照1792641594确认271个冻结文件一致，
严格确定性日志已产生、正在索引。配套恢复native-cached-resume-1792641554233412000，
worker46619/start23164742，等待父正常完成，reference/resumed各上限10800秒。
尚无新CUDA单步、反传及跨进程恢复通过结论；training_admissible=false、
formal_g2_pass=false。不同硬件/软件栈之间的逐位等价也未由此证明。

## 历史实现与证据

依据TRAINING_SPEC §4.3–4.4保持完整局划分、原始FAIR数据和冻结A监督，
只减少重复的整局解码及语义校验。不调整损失、采样、验证集或更新预算。

真实诊断来源为migration-integration-v1下
native-validation-profile-1792631598787696000。原验证循环每取一行都会
MigrationStore.load整局，因此358行验证局被重复解码、遍历校验358次。
三个代表行在cProfile下，batch耗时21.574/20.838/20.932秒；完整loss耗时
3.470/3.758/0.134秒。初始三局索引耗时121.404秒，涉及六次整局校验。
这些是带诊断开销的局部测量，不是训练吞吐或GPU时间占比。

VerifiedEpisodeStore继承原只读store，首次读取仍运行原
完整解压、语义哈希、逐帧合法性和终局校验；只保存最近一局的内部解码结果。
缓存键为dataset/episode语义/文件三个身份；命中仍读取压缩文件并核验SHA。
外层load保持manifest、回执字段、行数、attempt和quarantine检查。
load返回整局深拷贝，load_rows只返回所请求行的深拷贝，调用方修改不会改变缓存。
逐帧教师目标实时重算保持由原dataset执行。

现已通过独立payload构建接入SeriesMigrationDataset和选优训练入口。单局store A/B
只证明解码缓存局部收益；原生端到端训练和跨进程恢复仍待新独立部署验证。
不改变此前超时结论，不修改原冻结部署或Full128搜索任务。

首轮测试因篡改测试共用module级fixture而串扰：2通过/4失败，保留原XML。
修正为每项复制独立临时数据后重跑：6通过、6.99秒，生产缓存逻辑未更改。

## 真实整局读取A/B

native-cache-ab-1792631964890237000在.35完成，exit0。相同358行验证局，
三轮交替baseline/cached，每次比较完整整局语义摘要及回执，一致。
首次缓存加载15.800秒；后续原读取中位数13.405秒、缓存读取2.148秒，
局部倍率6.241。命中仍对压缩文件做完整SHA并深拷贝解码结果。
未测端到端训练倍率；不把单局暖缓存结果外推为多局随机访问收益。

## 按需取行及独立训练集成

build_payload.py从原父attempt固定SHA包构建新版本，只改变series_training、
新增verified_store、训练入口进度事件及验证进度日志四个文件。series的实现身份
增加缓存源码SHA，故新checkpoint绑定该实现，不跳过旧的身份校验。
batch保持原请求顺序，按局收集offsets，逐局load_rows后恢复原行顺序；原始文件
SHA、回执、整局划分、教师实时重算和监督损失均保持。缓存不保存教师或学生特征。

8项专项通过、18.07秒：完整帧及mask、真实梯度与AdamW严格等同原实现；
fixture保存/恢复后的RNG、下一批、选优历史、模型/优化器严格一致；错误split拒收。
首轮7通过/1失败是独立模块SelectionConfig类型不同的测试接线问题，已修正，
保留tests-cached-training.xml；通过记录为tests-cached-training-config.xml。
这些不代替CUDA新进程恢复实证。

.35独立attempt native-cached-round0-1792632378773664000已启动，supervisor45616，
启动时钟22247270；沿用已验收工程三局、batch256、microbatch1、FP32、单更新。
新任务上限5400秒，给完整验证和仍逐条件计算的梯度留有有界预算；旧1800秒失败
不更改、不续跑。启动载荷及四个变更的前后SHA保存在launch与integration-changes。
本次不是正式G2，训练准入仍false；原Full128搜索局及其冻结源码保持不变。

新独立start_resume/resume_worker保留原先完整父身份检查及逐阶段子进程清理，
等待上限由父预算+300秒派生，各对照阶段10800秒，预算写入settings并固定SHA。
native-cached-resume-1792632497065870000，worker45744/start22259033。
只在父成功完成单更新后进入对照；仍无原生恢复通过结论。

## 按需取行真实A/B

native-row-cache-ab-1792632538485401000，exit0。相同358行验证局的offset
0/179/357，三轮交替baseline/cached，选中三行语义摘要与回执逐次一致。
原读取后取三行中位数13.442572秒，命中load_rows中位数0.028252秒，局部475.804倍。
首次缓存加载15.751388秒。新模块SHA为
15c657cd07392450256669dff45b522aee4a586a666088e990feccdfc40e4e34，
与新训练冻结部署一致。报告episode_semantic_sha字段沿用早期探针名称，本次
实际摘要范围仅selected_offsets对应三行；不是整局语义摘要。
该范围包含文件哈希、回执检查及返回行复制，排除逐行教师重算、KL、反传、
索引和首次加载，不作为总体训练倍率或全数据访问分布收益。

## 独立执行链验收

verify_execution.py接受外部固定的父/恢复launch与下载回执四个SHA，复核归档
每个文件、冻结部署覆盖、父载荷、配置与数据入口、父单步checkpoint及其复制件。
从归档源码重算selection/series实现身份，要求四份checkpoint均绑定同一实现。
reference.before须与父checkpoint逐项重算一致，两个不同进程记录、阶段退出码、
midpoint/reference/resumed及数据身份相符后，调用独立checkpoint内容验收器。
通过整局数据重新计算身份与train/validation/test完整行序列，验证教师身份。

可选--native-data-attempt进一步调用已有原生数据验收器，严格绑定同一证据目录。
未指定时报告native_game_provenance_reverified=false，fixture不能冒充原生来源。
即便通过也只是执行归档、数据来源和恢复限定证据，不关闭G1/G2或授予训练资格。
本地验收工具未写入运行中冻结部署，避免改变正在执行的实现身份。

tests-execution-cache-final.xml：15通过、38.49秒，含缓存/训练/恢复以及执行链
篡改拒收；与先前8项/6项范围重叠，不相加。原生对照任务尚待执行，尚无其PASS。

native-data-reverified-1792632965594305000.json重新调用原完整原生验收器通过：
三局1044行，数据身份1d909bbd63a4eccb6ab0b913bd1f63bdcb87cb0141656407fb13e4847bab5255。
检查来源/协议、完整终局、双方预约轨迹、数据划分、所属引擎清理和旧工程更新证据；
不代表正式1280局、WDL校准或实战能力通过。

快照1792633057：训练45618活跃、冻结部署相符，验证160/358行已处理，日志累计
497.382秒。尚无checkpoint或更新完成标记。搜索快照1792632925：49组/98根已提交，
tick2018玩家0搜索中。门禁继续保持G0/G1进行中，G2–G8未运行。
