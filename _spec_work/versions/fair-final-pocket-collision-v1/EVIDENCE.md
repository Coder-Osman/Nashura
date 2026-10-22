# 摧塔后合法加农炮冲突：限定原生证据

核对日期：2026-10-21。范围由CONTRACT.md预先限定；不训练、不改变总门状态。

## 通过的尝试

- attempt：native-pocket-collision-1792566742281773000。
- 上传349个载荷文件逐项SHA核验；冻结来源A SHA为
  e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015，
  引擎SHA为49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba。
- seed78101907、执行采样seed78101947。沿11个合法边界推进到tick460，
  玩家1左公主塔HP=0、active=false，双方同时ready。
- 加农炮模型cell326/211对应native格(2,18)/(4,20)，3×3矩形正面积相交。
  初始合法区域无此交叠；摧塔后静态合法区域有731对，仅用来生成诊断候选。
- delay_slot1/30分别到期于tick462/520；两组双方原生命令均executed。
  每组从根快照重复续演两次，共4次，FAIR、clock、native digest和执行RNG一致。
- 关闭旧transport、建立新连接并正常reset后，完整重演11个边界至诊断根，
  FAIR、公开塔状态、最终clock/RNG一致；跨reset不比较native digest。
- 探针总耗时58.8961312790052秒。所属引擎和launcher退出，端口29744关闭，
  cleanup.clean=true；本地收集目录约1.939MiB，不作为正式局存储估计。
- verification.json为passed_limited_scope：2组、4次续演、0拒绝、0单方ready，
  general_pending_gate_closed=false、training_admissible=false。
- tests-final.xml：27项通过，零失败/错误/跳过，1.52秒；包含4项候选/几何测试、
  1项真实证据正例和22项篡改拒收。不与早期4项或其他测试数量累加。

## 保留的失败尝试

1. native-pocket-collision-1792566312625654000完成2组4次续演并清理，
   但漏存根公开塔证据，独立验收未通过。原报告、载荷SHA与源码保留。
2. native-pocket-collision-1792566556129938000在fresh-reset入口沿用单tick
   transport，90tick预热触发校验而中止，exit1，无完整report.json。
   所属引擎清理通过。后续只修复探针重连入口，不修改生产transport或其校验。

## 结论与边界

本结果只证明该合法摧塔轨迹、两组预约的原生命令被接受执行及重复恢复一致，
不证明建筑实体最终仍处于交叠坐标。“几何冲突造成私有拒绝”的假设未获支持。
未观察到合法拒绝或单方ready，不构成其全局不可达证明；一般pending信念重建、
Full128与恢复仍待验。无搜索、无完整对局终局，不关闭G0/G1，不新增总门PASS。
运行中完整搜索局244个冻结生产Python文件保持原身份，新旧结果严格隔离。

## 验收身份

- verifier SHA：06e05b22752c6b5280d5b7ee2acfe45c08e725da378c298c64df8b842a0a9059。
- attempt.json SHA：35aaa2b6f4ebe6839fb679c61b340c2e6ae5ec3cd50f58a9cd0fe839caea8726。
- report.json SHA：f8c420c32220af466a68b90ab536218de1332642b88678e73348bd9a46f4a4ba。
- cleanup.json SHA：2081e692c567a52701281a29b009e273c34966afcb8d3057f47f6b69bea965b2。
