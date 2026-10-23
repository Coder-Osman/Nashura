# 验证选优checkpoint到补充采集的独立接口

## 2026-10-23 terminal选优到采集包交接

build_collection_payload.py新增独立selection模式，要求execution、completion外部SHA
及完整父数据路径；先执行verify_selection_execution的真实内容验收，再读取已核验
handoff和冻结payload。输出继承训练源码、确定性运行时及配置原始字节，仅沿用
已有采集侧更新范围。旧launch模式保持原接口；两种父身份互斥，缺参数或runtime
覆盖在创建输出前拒收。完成验收后再次核对执行/完成文件与selected.pt身份，
handoff和payload仍通过原校验器重读，过程中的修改拒收。

新report使用FairFinalSelectedCollectionPayload/v1，包含父execution、completion、
checkpoint SHA及独立验收内容；training_admissible、formal_g2_pass、gates_closed
均为false，requires_registered_tasks仍为true。本入口只构建采集部署包，后续
仍须完整种子登记、真实collector预检、原生完整局及独立验收，不自动启动引擎。

35项交接与旧构建回归通过（8.82秒）；联合选优执行/内容、构建、worker测试
78项通过（58.28秒），集合不相加。成功交接单元用显式mock隔离验收器，真实
不合格终端归档通过实际验收器拒收；不将mock成功称为原生父结果通过。
后台真实terminal选优仍运行，真实交接及round2原生尚未执行；Full128冻结部署保持。

## 2026-10-22 旧策略screen显式种子补验

audit_policy_seed_history.py只读固定SHA的旧400局策略评测status/export，核验
五机124分片、完整200对换边覆盖、退出/引擎关闭及导出模型身份。比赛种子
从结果显式seed JSON Pointer提取；seed_base与pair仅用于一致性检查。
旧结果缺逐局完整terminal证据及执行代码SHA，因此
execution_code_identity_proven=false、native_terminal_reverified=false；只用于
保守排除，不宣称重新完成原生终局或历史事前登记。

以此前sampling清单为基础，仅扩展evaluation 200个不同种子：training 1136、
value_training 4096、evaluation 328，共5560个，三类交叉重叠为空；训练和
价值清单原字节保持。产物位于historical-seed-inventory-policy-screen-20261022。
独立CLI支持显式--source-path，不依赖测试模块；相同输入重入字节保持。

新25项专项通过，联合136项通过1.77秒，含此前111项，范围不累加。覆盖19类
结果损坏及4类基础来源损坏拒收。policy-screen-seed-history-batch-evidence-20261022.json
固定25项来源/实现/测试/产物SHA与字节数，逐项回读一致。
嵌套策略评测及更早实验仍待核验；historical_census_complete、
historical_preregistration_proven、training_admissible和formal_g2_pass保持false，
未创建正式采集登记，不新增整体门或限定PASS。

## 2026-10-22 教师采样历史显式种子补验

新增audit_sampling_seed_history.py，只读固定SHA的五机汇总结果及实际
evaluate_teacher.py，核验五份manifest与run_host.py代码身份、任务覆盖、
模型/配置/32模拟/单信念世界/采样温度/根噪声及原生完整终局。两组各64对
换边结果共256条，从每条结果的显式seed JSON Pointer取得64个不同比赛种子；
seed_base加pair只用于一致性检查，不作为排除清单的种子来源。

重新核验旧审计、三类清单及原来源后，仅扩展evaluation；training和
value_training原字节不变。最终historical-seed-inventory-sampling-20261022
包含training 1136、value_training 4096、evaluation 128，共5360个不同种子；
三类交叉重叠为空，重入后字节保持。新增专项18项通过，联合111项通过
2.113秒，范围重叠不相加，也不与其他专项的同名测试数量混计。

sampling-seed-history-batch-evidence-20261022.json固定59个实现、测试、来源
及产物SHA/字节数，逐项回读一致。仍属部分历史盘点：嵌套策略评测及更早
实验待核验；本地结果与代码记录不证明历史事前登记或原始远端重验证。
未创建正式采集登记，historical_census_complete、historical_preregistration_proven、
training_admissible和formal_g2_pass保持false，不新增门PASS。

## 2026-10-22 远端历史轮次来源补验

只读收取.35历史continuous第1/2/3轮原始manifest，各124个显式match_seed任务；
逐份与此前remote-seed-sources.json中的对应文档核对。第1轮与已审阅本地清单
内容相同，不重复作为排除来源；第2/3轮新增248个不同训练种子。当前远端回读
不证明当年事前登记、实际任务完成或历史执行代码身份。

新增audit_remote_seed_history.py，调用方固定收取回执及旧bundle的外部SHA；
核验三轮完整来源集合、固定路径、原始SHA/字节数、显式uint32、文档规范字节
一致性及第1轮本地对照。JSON数值类型保留，整型与相同数值浮点不视为同一来源。
复用现有显式指针/来源核验与不可变清单发布；完成前再次检查来源，最后发布
remote-review-complete.json。原始历史文件和运行中冻结部署不修改。

最终产物：historical-seed-inventory-remote-final-20261022，training 1136、
value_training 4096、evaluation 64，共5296个不同种子；三类交叉重叠为空。
真实产物重入后全部字节不变。首次工程产物historical-seed-inventory-remote-20261022
保留，仅最终目录对应收紧数值类型比较后的实现。

tests-remote-history-and-sources-final.xml：101通过、56.06秒，含此前87项及
14项远端核验测试。首轮100项为收紧数值类型前范围，均不相加。覆盖SHA/字节/
路径/重复或缺轮/旧bundle不符/数值类型/本地首轮冲突/错误准入及过程中改动拒收。
证据汇总见remote-seed-history-batch-evidence-20261022.json。

嵌套评测、教师采样评测、更早实验仍待核验，不宣称全项目盘点完成，不创建
正式G2登记；historical_census_complete、historical_preregistration_proven、
training_admissible和formal_g2_pass保持false。

## 2026-10-22 真实历史种子首批盘点

audit_local_seed_history.py只读六份明确审阅过的历史manifest，逐项读取jobs中
显式seed或match_seed，核对schema和uint32类型。不会把训练RNG、seed_base、
pair编号或旧seed-audit的汇总数量当作比赛种子。任务是否完成不影响保守排除。
换边重复种子归并，跨scope重叠显式报告，不删除重叠来伪造互斥。

产物位于evidence/fair-final-v1-20261019/historical-seed-inventory-local-20261022：
training 888、value_training 4096、evaluation 64，共5048个不同种子，
三类交叉重叠为空。三份FairFinalSeedExclusion/v1清单及显式JSON Pointer
source-map接入现有v2来源核验器，逐文件SHA与实际提取集合核验通过。
audit.json最后不可变发布，绑定来源、清单及实现SHA；原历史文件不修改。
相同输入重入保留原字节，来源变化或预检后篡改不会产生新完成标记。

tests-history-and-sources.xml：87通过、38.39秒。包含下述77项及10项新历史
盘点测试，范围重叠不相加。新测试覆盖显式种子缺失/错误类型/schema、换边
归并、未核验评测日程排除、重入、原始来源变化、核验前篡改及跨scope重叠。

这是局部真实历史盘点，不是完整历史登记。嵌套评测日程、教师采样评测、
远端bundle中的其他轮次及更早实验仍待结合执行代码和结果补验。没有从基数
推断实际比赛种子，也未为正式G2创建登记；historical_census_complete、
historical_preregistration_proven、training_admissible及formal_g2_pass均为false。

## 2026-10-22 种子来源文件核验（登记v2）

seed_sources.py补齐先前只核验source_shas格式的缺口。登记升级为
FairFinalCollectionSeedRegistration/v2，旧v1登记不会被新入口接收；
运行中冻结部署保留原身份。登记与采集CLI均新增必填--seed-source-map和
--seed-source-artifacts，仍仅开放独立engineering-acceptance入口。

来源映射为FairFinalSeedSources/v1，严格包含schema与sources；sources每项
包含file_sha256与非空、唯一的engine_seed_pointers。示例：

```json
{
  "schema": "FairFinalSeedSources/v1",
  "sources": [{
    "file_sha256": "<历史JSON文件的64位SHA256>",
    "engine_seed_pointers": ["/private_tasks/任务ID/engine_seed"]
  }]
}
```

每个指针采用JSON Pointer语义，可定位单个uint32或uint32列表；支持键名转义，
拒绝布尔值、浮点数、越界、错误路径和非规范数组下标。换边任务重复使用同一
种子允许在来源中出现，归并后必须与相应外部清单的engine_seeds精确相等。
来源集合须与全部清单声明的source_shas精确对应，漏文件、多文件或重复文件拒收。
来源路径不参与身份，文件移动和输入路径顺序不改变登记；来源映射原始字节、
语义摘要及每个历史文件的SHA/字节数均进入登记。

采集在模型加载和原生引擎访问前重新核验全部来源；私有审计归档前再次核验，
只使用该次已验证的原始字节发布source-map.json、sources/<SHA>.json、外部清单
及最后的registration.json。原始文件、映射、清单或登记的已有归档冲突均拒收。
private-inputs.json另记来源映射SHA和seed_sources实现SHA；这些字段不进入FAIR输入。

适用范围为“调用方提供的历史文件及显式JSON指针”。调用方仍可能漏列历史实验
或未指向文件中的全部比赛种子；本检查不证明盘点完整、scope分类语义、可信时间
或旧任务事前登记，也不验证历史模型/执行日志的完整上游来源链。正式G2仍须先
完成历史盘点及独立原生入口验收，未因来源检查而放行。

tests-seed-sources-final.xml：77通过、35.55秒，包含来源专项、登记专项及8项
既有选优采集接口测试。覆盖原始来源与映射身份、明确指针提取、错误类型、
路径搬移/重排、预检后改动、模型/引擎前拦截及四类不可变归档冲突。
此前72项与36项报告是增强前或重叠范围，不相加。

## 2026-10-22 外部种子登记与采集前拦截

本节为v1历史范围；当前入口的新增来源核验与v2身份见上节。

新增seed_registration.py，依据TRAINING_SPEC §4.3–4.4与ACCEPTANCE_GATES G2
的整局预划分和独立评测要求，为独立工程入口固定当前series前缀、当前轮次
manifest及全部任务私有种子身份。登记不生成或替换种子，不修改模型、训练损失
或轮次配额；现有运行中attempt冻结部署保持原样。

外部清单使用FairFinalSeedExclusion/v1，严格包含schema、scope、engine_seeds、
source_shas四个字段。scope覆盖training/value_training/evaluation，分别声明
已有训练、价值训练及评测种子；每份清单保留非空来源SHA列表。原生比赛种子
为uint32，拒绝bool、越界和重复；外部种子必须全部进入series排除集，且与
当前series所有轮次不重叠。原有全历史、换边配对及划分校验继续执行。

登记CLI强制engineering-acceptance，使用ensure_immutable原子持久发布，
相同内容可重入，改写已有登记拒收。登记内容绑定清单原始文件SHA和语义SHA；
清单重排不影响身份，但字节改变、漏清单、换来源或错计划均拒收。

collect_selected.py新增必填--seed-registration、--seed-registration-sha、
--excluded-seed-inventories。先验证登记和外部清单，再加载模型/数据，最后
连接引擎。每次attempt在私有audit区保存登记和全部原始清单，保存前再次
核验文件SHA；若预检后文件改变，在访问引擎前停止。归档按SHA命名且不可变，
private-inputs.json记录登记SHA、清单SHA和登记实现SHA。FAIR输入无新增字段。

低层prepare_selected_task仍只负责模型与父历史校验；强制登记由独立CLI承担。
测试调用低层函数不等于正式采集入口已完成原生验收。

tests-seed-registration-archived.xml：36项通过、35.32秒，其中28项新登记/
归档专项和8项既有选优采集测试。覆盖round2身份、完整历史、外部重叠、清单
字节/来源/种子篡改、类型、不可变重入、预检与归档间文件变化以及引擎访问前
拦截。此前tests-seed-registration.xml的34项为归档增强前的范围，不相加。

范围限制：只验证调用方提供的清单，不证明历史种子盘点已完整，也不核验
source_shas所指历史产物的全部来源链。登记文件本身不证明可信外部时间或
历史采集事前性；新采集启动时须冻结登记SHA及实际启动凭据。尚未为正式G2
生成登记、启动正式采集或在原生round2运行新入口，不新增整体门PASS。

## 2026-10-22 嵌套历史评测补验

四组旧评测16份来源只读回收，回读前后SHA一致；审计完整核对3200条换边结果、
模型/执行代码记录和显式seed。新增1600个评测排除seed，当前共7160个：
training 1136、value_training 4096、evaluation 1928，三类交叉重叠为空。
训练/价值清单原字节及不可变重入保持，独立重算全部产物逐字节一致。

5条draw_or_unexposed_tiebreak只记录不明确结果，不升级真实平局；本批不接收
任何WDL标签。专项54项、种子登记与历史核验联合190项通过（4.33秒），范围
重叠不相加。源码、XML、来源/产物SHA清单及范围见NESTED_SEED_EVIDENCE.md和
nested-seed-history-evidence-20261022.json。

存储代码身份匹配不证明旧进程或原生终局已重验，不重新计算强度区间。
历史全项目普查、未执行嵌套计划及正式G2事前登记仍未完成，不新增门PASS。

## 原有接口与历史证据

依据TRAINING_SPEC §4.3–4.4，衔接现有migration-selection与原生supplement collector。
只作为G0/G1接口工程，G2正式1280局及后续评测仍受前置门禁约束。

collect_selected.py接受一份外部固定SHA的私有series计划、完整父轮数据目录、
已终止轮次的选优checkpoint与SHA、原训练损失及验证配置。先检查全部历史
回执/划分/种子与下一轮任务，再加载验证选出的best网络；未终止轮次、错SHA、
遗漏历史轮次、错数据父身份、错误下一轮行为模型及种子重叠均拒收。

实际比赛seed和执行seed只能取自已注册任务，入口没有临时覆盖seed的参数。
它们保存在私有审计区；FAIR模型输入与既有collector一致。只从真实完整终局发布。
下一轮训练仍使用selected_parent继承选中网络及对应优化器，采集入口只读模型。
独立入口复用原生预约/隔离/终局实现，不修改旧入口或任何运行中冻结部署。

当前入口强制engineering-acceptance，最多三次独立尝试；失败保留现场，外部调度
负责所属引擎的启动/清理。此接口本身不声称外部全项目种子登记完备、轮次协调、
原生round2或连续梯度验收通过。后续须以实际三轮链路和最终固定SHA证明这些条件。

## 2026-10-22 冻结训练身份的后续采集载荷

build_collection_payload.py从外部固定SHA的父launch及payload构造独立采集包，
逐项核对规范tar成员、部署文件集合与源码SHA。训练、选优、series和配置保持
父包原字节；仅更新collect_selected.py并补入seed_registration.py/seed_sources.py。
确定性父包在登记预检后、teacher加载前配置同一冻结deterministic_runtime，
并将配置及运行时SHA写入私有审计；旧非确定性包保持原运行方式。

新7项专项覆盖父身份/包/部署/运行时损坏拒收、训练源码保持、可复现打包与
实际解包入口加载；与原采集接口联合15项通过、62.53秒。最终builder增加证据
字段后重跑7项通过、3.41秒，见tests-collection-payload-final.xml；范围重叠不相加。

实际构建collection-payload-deterministic-20261022，包SHA为
9a2b83f07840453384a8de7b81e86e2fcc00b7e328cf9e5b92192dba2184696a。
独立解包复核273个源码条目及部署清单，270个父文件字节保持，collector --help
正常退出。verification.json固定builder、测试、XML、包及报告身份。

这只是本地准备：未部署、未创建种子登记、未采集原生局，也未将单步父checkpoint
当作terminal选优父。仍须完整父历史、合法轮次终止、选优身份与任务登记通过后
才能启动后续采集。training_admissible=false、formal_g2_pass=false、gates_closed=false。
未修改任何远端运行中的冻结源码，不新增整体门或限定PASS登记。

## 2026-10-22 工程结果

tests-selected-rounds.xml：8项通过、38.18秒，包含首批7项。fixture实际训练选优
checkpoint用于加载测试，验证round1/round2的best网络、冻结模式、RNG与父数据
保持；错误计划SHA、checkpoint身份、行为模型、seed重叠、未完成轮次和历史缺失
均在原生引擎访问前拒收。该结果不是新入口的原生完整局验收。

start_native_selection.py在.35启动独立原生数据消费attempt
native-selected-round0-1792629813912401000，复用既有三局已验收工程数据，
以新模型/AdamW、完整验证集和单次更新预算验证train_round真实入口。
源A、部署包、源码及命令写入launch.json；supervisor44892、训练44894，
均记录/proc启动时钟。最多1800秒后停止所属训练子进程，无引擎也不终止旧任务。
单步完成仍不是terminal选优父轮；须按身份恢复到合法轮次终止后才允许补充采集。
本attempt沿用既有数据事后登记，不宣称旧seed获得了事前预注册证据。

## 2026-10-22 后续采集的有界引擎生命周期

新增selected_collection_worker.py，运行前固定请求、部署清单及所有源码SHA。
先执行collect_selected.py --preflight-only，完成种子/完整父历史/terminal选优
模型和规则校验后，才启动本次独占引擎；预检不创建数据store、私有审计或连接
原生引擎。实际采集重新执行同样校验，保留严格身份门禁。

worker固定launcher/service/probe身份，核验运行中的PID/start_ticks和ready
attestation；预检、启动、采集均有独立有界预算。正常、错误、超时或中断后仅
清理自己的launcher及匹配启动身份的原生子进程，核对端口释放和干净退出。
缺报告或清理失败不写成功；completion仅为collected_requires_independent_verification，
不替代整局内容与来源独立验收。occupied端口不终止其他进程。

最终三个文件联合tests-bounded-collection-final.xml：43项通过、60.57秒，
范围包含此前41项worker/载荷/采集及后来新增的预检用例，重叠不相加。
子进程/socket夹具覆盖正常、预检/采集失败、各阶段超时、身份损坏、占用端口、
SIGINT和清理失败；这些不是原生完整局证据。

新独立采集包278源码SHA复核、273父文件保持，仅更新采集、登记、来源、worker
及launcher五文件。payload SHA为
d8f59a0d9a3333dac3207b911951a41afb92b3f95c751c9d5222d0013a647dfe。
见bounded-collection-payload-evidence-20261022.json及
bounded-collection-verification-20261022.json。尚未部署或采集，缺terminal父时
不构造虚假父凭据。运行中的冻结训练/搜索保持原样，所有准入标记仍为false。

## 2026-10-22 归档后续采集独立内容验收

verify_selected_collection.py只读取显式映射的本地归档，不访问request内远端路径。
外部固定request、deployment、completion、plan与selected SHA；重新核验冻结
源码、规范路径与symlink、完整父数据和terminal选优、逐局FAIR/动作/生命周期/
终局标签、receipt/attempt、原始terminal及报告、私有输入和种子来源JSON Pointer。
预检及采集必须正常退出；所属launcher/child的PID/start_ticks、引擎attestation、
ready、清理与端口释放一致，失败及清理失败归档拒收。

运行时声明必须绑定部署内deterministic_runtime.py的SHA，并逐字段核对类型、
确定性/TF32/CUBLAS设置和torch/CUDA版本字段；此项核验归档声明，不重跑CUDA。
多轮父数据只读重验支持1–3轮，保持真实dataset/series身份与继承optimizer步偏移。
本次联合47项通过65.794秒，覆盖重新封装后的内容/身份/路径/运行时破坏拒收；
小模型及采集记录属于fixture，不代替原生完整采集。

证据见selected-collection-verifier-evidence-20261022.json和
tests-selected-verifier-final-20261022.xml。通过范围仅归档内容一致性；不重新计算
教师标签、不宣称原生采集/正式G2/门闭合，所有训练准入标记仍为false。

## 2026-10-22 合批父包的确定性来源交接

新合批训练包包含deterministic_runtime及训练入口配置，但launch没有旧版
deterministic字段；旧采集builder严格拒收training runtime identity differs，
保持该默认拒收行为。新增显式runtime-parent及其外部固定launch SHA，逐项
核验祖先launch/payload/部署、确定性声明、直接父引用、运行时原字节与训练
入口配置。声明冲突、错误祖先、缺SHA或运行时变化仍拒收，不根据文件存在推定。

仅在独立采集包接入确定性配置与私有审计，未修改运行中的launch或源码。
真实包collection-payload-batched-20261022共276个源码条目SHA独立复核，
273个父文件原字节保持；解包后的采集CLI可加载。载荷SHA为
670179f80d93ccd35eccb9d8994229977ad86359d78395e1a28f8675471ada5d。
联合24项通过98.76秒，覆盖真实父/祖先、可复现打包、旧入口兼容与损坏拒收。
见tests-collection-runtime-lineage.xml及collection-runtime-lineage-evidence-20261022.json。
仍仅本地准备，未部署、登记或原生采集；不把在跑单步父当作terminal选优父。

## 原生连续更新与新进程恢复对照（历史）

probe_native_resume.py复用冻结selection/series/训练器，在同设备加载父单步checkpoint。
reference进程再做两次真实更新：中间保存midpoint，随后不退出继续更新；resumed
新进程从midpoint恢复并重复后一次更新。严格比较模型、AdamW、所有RNG、采样顺序/
游标、下一批行、验证历史/best选择和指标；冻结教师保持不变，优化行仅来自train。
两进程各自重新完整验证best记录，不省略已有恢复身份检查。

start_native_resume.py独立克隆父部署包，新增探针与worker，不修改父运行源码。
有界worker核对父PID启动时钟，等待父进程退出且completion exit0后，锁定父报告
与checkpoint SHA、复制独立父快照，再依次启动reference/resumed。父失败即停止。
等待预算2100秒，每阶段3600秒；仅管理自己的训练子进程，失败保留全部产物。
attempt native-selected-resume-1792630271097021000，worker45071/start_ticks22036431。
快照1792630470.028989确认worker活跃、处于waiting_for_parent，冻结部署SHA一致。

verify_native_resume.py不只读取成功标记，直接重新计算三个checkpoint的网络/
优化器/RNG/游标/选优摘要及嵌套语义摘要，并比较报告与实际内容。其输出明确
native_deployment_verified=false，后续还须核验原生数据、部署和进程证据。
tests-native-resume-verifier.xml：2项通过、15.03秒，包含原探针测试范围，不累加；
实际fixture梯度与恢复对照通过，重算报告文件SHA后的错误优化器摘要仍被拒收。
原生恢复任务尚未执行到对照阶段，当前没有原生恢复PASS。

## 2026-10-22 有界训练失败与归档

父attempt在1800.566秒达到原上限，以124退出；没有round0.pt、更新报告或训练
日志。恢复worker检测父失败后退出，phases为空，没有启动reference/resumed。
快照1792631641确认父supervisor、训练和恢复worker均已退出，冻结部署保持一致。

新增collect_native.py仅在相关进程退出且有终止证据时收取。校验启动清单中的
冻结源码；恢复attempt额外从固定SHA的父payload读取部署清单并逐项核验。
文件按SHA收取，相同归档保留原回执字节，变更拒收；失败日志也归档，输出明确
terminated_archive_only、native_resume_verified=false，不自动生成恢复PASS。
两份native-download-receipt.json分别记录274/276个文件。联合归档/恢复测试
8项通过、15.04秒；进一步补充父包正例后归档专项6项通过、0.06秒，范围重叠。

profile_native.py为独立只读诊断：使用原冻结部署、三局工程数据与源A，分别测量
数据索引及验证集开头/中间/结尾三帧的数据加载与完整条件损失；不更新权重、
不修改运行源码、不写原数据。独立进程上限300秒，保留profile结果和失败退出码。
该诊断尚不构成吞吐验收，也不代表G2。
