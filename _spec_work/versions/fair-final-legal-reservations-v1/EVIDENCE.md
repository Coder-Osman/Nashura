# 合法预约探针执行证据

核对日期：2026-10-21。attempt：native-legal-reservations-1792565102095525000。

## 实际结果

| 根 tick | 分支数 |
|---|---:|
| 90 | 18 |
| 110 | 14 |
| 2598 | 13 |
| 5602 | 12 |

共57组分支、114次续演，49组单边 ACT、8组双边 ACT；沿原轨迹重演156个
边界。probe 耗时236.825633秒。每组两次恢复续演一致，所有动作经过合法
掩码校验；模型未变，快照释放检查通过。未观察到拒绝或单方 ready。

worker退出码0，launcher与所属引擎进程退出、29743端口关闭。
独立verification.json为passed_limited_scope，明确保留一般pending门。

候选专项原先单独3项通过；最终合并候选与验收器测试22项通过、零失败/
错误/跳过，1.844秒。22项包含上述3项和19项真实产物/篡改验收测试，
二者不累加；也不与其他专项或总门子项相加。

## 可复核入口

- attempt.json：上传348文件的SHA、runner与history manifest身份。
- report.json：四根合法掩码、预约、每tick outcome、结算证据和重复续演。
- cleanup.json：所属进程及端口关闭证据。
- verification.json：独立验收结论、verifier SHA及上述三文件SHA。
- tests-final.xml：本版本候选及验收器22项测试。
- CONTRACT.md：输入、候选选择与结论边界。

本实验既没有找到合法拒绝反例，也没有证明一般合法拒绝不可达。需要继续
取得一般pending分支的原生重建/Full128/恢复证据，或严格的不可达依据。
未新增整体门PASS，未产训练数据，未启动G2/G3或扩大actor。
