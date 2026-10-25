# 全局随机快照搜索集成：原生四根限定验收通过

## 2026-10-25 原生收尾及实际独立验收

同一worker已停止，369份归档与所属双引擎清理通过实际独立内容验收。
tick110与后继根各做一次真实隐藏状态干预配对，共4根、512次有效模拟；
完整native+2504字节全局MT摘要逐根恢复，1483次创建/1483次释放、551次恢复。
探测耗时2078.438453秒，两个隐藏干预配对的搜索结果一致。
身份与范围见native-evidence-summary.json；原始审计和归档私有保留。

首次直接CLI调用误载主工程native_runner，在FAIR历史字段校验时拒收，未发布
验收结果。保留verification-initial-failure.json；随后新进程先固定history-v2
包，实际运行原字节verify_search.py，验收通过。未修改冻结验收器、归档或条件。
新增verify_isolated.py作为明确的版本隔离入口；使用相同attempt和外部SHA参数，
只在新输出尚不存在时执行，旧验收结果不覆盖。全局搜索并非完整搜索局，
pending/fallback和全部随机覆盖仍未闭合，G0/G1及34/43登记保持开放。
以下为历史运行和准备记录。

## 2026-10-25 连接恢复后已启动

原生native-global-search-1792836650700032000在核查原任务后独立启动，
worker73018/start49197529，端口29791/29792，预算5500秒；launch SHA256
`58cecfb3cc240b686677af8a2f711944d75ea4ac5b0080e2c65bb7a28bc48fea`。
快照1792902971为running、completed_roots=1，四根及清理尚待完成，独立内容
验收未运行。下文prepared/SSH超时是先前状态；同一冻结准备包未重复派发。
Full128和terminal续跑独立保留，不据首根完成关闭G0/G1或随机全覆盖门。

## 原准备和失败连接记录

2026-10-24，新增独立验证入口，把已修复的全局MT快照引擎接入冻结history-v2
的general-provider local Full128搜索，不修改运行中的原Full128与选优部署。
启动前实际重验global-rng-snapshot-v1父原生归档，通过后导出353个冻结载荷。

当前准备包：native-global-search-1792836650700032000，preparation SHA256
62b5dd2a1942fdab70f14e4501ebd5935bedee004bb9a87fedc97b86ea39d186。
状态prepared_not_launched，不代表原生已经执行或通过。较早的准备包保留，
后续加强launch状态区分后已过期，当前源码身份校验会拒绝旧包。
实际执行launch入口在严格SSH连接阶段以TimeoutError退出；无launch.json、
无worker身份，不宣称已创建远端引擎。之后本机重验当前准备包353项字节一致。

新增传输审计核对每次快照创建、恢复、释放；完整摘要包含native payload与
2504字节全局MT，另核对generation/configRevision/stateEpoch。每根搜索独立
绑定日志区间及根摘要，拒绝用累计512次恢复代替四根各自的证据。审计只写
私有证据，不进入模型、动作生成、节点身份或信念条件化输入。

38项专项通过；与general-provider及global-snapshot联合139项通过，7.81秒，
tests-final.xml保留结果。包含真实父归档重验、真实构建载荷逐文件SHA核对，
也包含显式合成生命周期/传输fixture；这些测试不是新原生搜索运行证据。
tests-initial.xml记录最初31项通过，集合有重叠，不累加。
公开回归770项通过、26项因缺少私有附件跳过（95.76秒），主工程211项通过
（51.03秒），2212项公开源码清单核验通过，凭据扫描零发现。集合不相加。

同日五台已授权训练机SSH TCP22均超时，.35状态检查提权后仍超时；只读路由
显示走en0默认网关。该证据不证明远端任务停止，不据此重启、终止或替换任务。
原Full128与terminal选优的当前状态均待连接恢复后核验，之前存活快照只作历史。
GitHub API正常，远端main仍为085ababc07f2ea13a90342bafe8b24e9c918ce55。

计划原生验收为tick110及后继根、两次真实隐藏干预，共四根各128模拟；需要
完整根恢复、逐根完整摘要、独立信念、512有效模拟、句柄释放及所属双引擎清理。
一般pending/fallback、完整搜索终局和全部随机覆盖仍未通过；不升级34/43、
G0/G1或正式训练准入。正式G2–G8与五机放量未启动。

连接恢复后的操作顺序：先读取原Full128与terminal的同一PID/start_ticks和完成
文件，再核验该准备包SHA后启动独立任务；收取停止的同一worker归档并独立运行
verify_search.py。任何观测超时仍按未知处理，不自动另开同局或补造成功回执。
