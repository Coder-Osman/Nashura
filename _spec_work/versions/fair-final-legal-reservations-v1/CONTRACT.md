# 合法预约原生分支探针 v1

## 目的与范围

在独立 history-v2 运行时上检查合法 ACT 的预约、到期结算、公开执行抢占与
快照恢复。旧 single-ready 探针曾绕过一次合法掩码注入支付不足动作，本探针
全程使用正常合法掩码校验，二者的证据范围分别保留。

本探针不进行搜索、模型训练或数据发布。源模型仅用于读取卡牌目录，始终
eval，前后参数身份相同。它不替代完整 Full128 搜索局或一般信念重建验收。

## 冻结输入与执行

- 来源 A：e908f642bf1fda7bf0dca79a8d20d73ec2f56788a57f5d53739f13e2ae58d015。
- phase 引擎：49098dbf5abe6a3e01dab5042f4f8c903337ef9a76e4cefa7808c4df013095ba。
- history-v2 manifest：1f552d1cbcd3384006967242eba80ede42a3d7720edd852f9e33a0f25c02e981。
- 固定轨迹：native-refill-trajectory-1792489561748498000/project/input-trace.json，
  SHA cb18af4dbcf642aee34ed7315c0884414d13d34849c092ac2f47581be153cf61。
- 预定根 tick：90、110、2598、5602。按归档轨迹重演并核对 FAIR 身份。
- 每卡选取 delay1 首个合法位置、delay30 最后合法位置；执行一方 ACT / 对方
  WAIT，并加入可用的同延迟双边 ACT。镜像炮位置只表示网格候选，未证明原生
  中心完全重合，也不将其称为碰撞测试。
- 每个分支恢复同一根两次，逐 tick 推进至首个完整决策边界，最多60 tick；
  两次结果须严格相同，原生 outcome 与预约结算证据须对应。
- 单引擎 .35:29743；probe/worker/SSH 上限为1200/1260/1450秒。启动内存
  64GiB、运行32GiB、磁盘5GiB仅为诊断保护，中心正式200GiB门槛保持原值。

## 独立验收

verify.py 根据持久化合法掩码独立核验动作、预约创建/到期、逐 tick 连续性、
native outcome 完整性、到期先结算、公开执行抢占、ready/pending 与恢复重复。
同时核验来源、部署身份、冻结轨迹、快照释放及所属引擎清理。
tests-final.xml 覆盖候选生成、真实产物正例和18类篡改拒收。

## 边界

仅覆盖一条固定轨迹上的有界候选。未观察到拒绝不构成合法拒绝全局不可达
证明；没有一般 pending 根的信念重建、Full128 或跨进程恢复证据。
general_pending_gate_closed=false，training_admissible=false。
运行中的原完整局244个生产文件不修改，新旧源码身份分别验收。
