# history-v2独立限定原生验证

## 目的与身份

补充独立新版源码的原生运行证据，不替代G0/G1整体验收。
本文件与运行时版本清单分离，避免更新进度使运行中的源码身份失效。

- attempt：native-history-v2-refill-1792559847329216000。
- 部署：341个生产、工具及规则文件，逐项校验SHA。
- 原生引擎：phase-snapshot，SHA为
  49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba。
- 端口：29741/29742，与旧完整搜索局29731/29732隔离。
- 初始边界tick110、玩家0及下一公开边界tick142；每边界改变真实隐藏牌前后
  各计算一次Full128，已完成4根、512次有效模拟。
- worker上限5500秒，搜索探针上限5400秒；保留32GiB内存/5GiB诊断磁盘保护。
  正式中心200GiB门槛仍未通过。

## 验收与范围

run_native_refill.py部署、执行并收集；verify_native_refill.py独立验收：
精确128访问、未完成边零回传、候选质量与行为采样、独立世界、历史v2、
隐藏配对一致性、增量游标、原生phase恢复与两引擎释放。
test_native_refill_verifier.py包含1个原证据正例及20类篡改拒收，须配置
FAIR_V2_NATIVE_ATTEMPT后运行，跳过不作为通过。

即使通过，结论也仅为passed_limited_scope：general_pending_closed=false、
native_rejection_reachability_proven=false、complete_game_verified=false、
training_admissible=false。轨迹没有己方拒绝证据，不证明完整策略proposal
在一般私下拒绝分支上的原生覆盖，也不证明经验proposal已总体校准。

## 当前状态

已完成收集与独立验收，verification.json结论为passed_limited_scope；
不计入已登记34个限定PASS子项，也不关闭整体G0/G1。
四根各128有效访问，共512次有效模拟，invalid_attempts与unfinished_edge_backups均0。
两组隐藏配对搜索结果严格一致，真实隐藏确已改变；四根耗时分别为
540.710196/535.568852/494.092458/489.639353秒，探针总耗时2087.656360秒。
增量游标为90→110、110→110、110→142、142→142；
四根局部重条件化history_ticks_replayed均0，不外推一般状态的重建成本。
worker退出码0，两台所属引擎29741/29742与launcher均已释放，清理核验通过。
部署341文件身份通过，原完整局244个生产源码文件保持不变。

独立验收器SHA256为
344a621ad914a847663063e03eddc3e4099b65957b65c373b72797a1980882b4。
native-verifier-tests.xml记录21项通过、零失败/错误/跳过，耗时1.722秒，
含原始证据正例与20类篡改拒收。测试使用本attempt真实产物，并非fixture替代。
运行中attempt缺少成功退出与完整产物时，validate明确拒收，未生成verification.json；
完成后才独立生成限定通过报告。

证据入口：native-history-v2-refill-1792559847329216000/verification.json、
同目录run-results.json及tick-110-seat-0/report.json、cleanup.json；
证据测试：本目录native-verifier-tests.xml。
一般pending、原生合法拒绝可达性、完整局、总体先验校准与训练准入仍未通过。
