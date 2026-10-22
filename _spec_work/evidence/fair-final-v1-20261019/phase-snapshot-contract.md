# 原生 phase 快照补全：工程验收范围

## 已复现的缺陷

native-local-reconditioning-1792484406477719000 证明：原生序列化恢复后，
对象 deploy_previous_ms 保留了续演中的值；同一根中的 1000 可变为 300/950。
原生 digest 未覆盖该字段，单独比较 digest 会漏检。恢复与局部换牌重置遥测
epoch，也会丢失当前 tick 的攻击、移动和部署 step 证据。

## 独立修复

- fair_final_phase_snapshot.inc 是独立新版构建的 sidecar，不修改旧部署库。
- capture 在暂停的原生执行线程中枚举全部对象，保存原生 ID、owner/index、
  部署 previous/remaining 值及当前 phase cache；不保存对象地址为恢复身份。
- restore 先验证相同比赛 generation/tick、完整对象集合、唯一 ID、对象类型和
  remaining 一致，再按原生 ID 绑定新地址，恢复 previous 并重绑定 cache epoch。
- 对象缺失/重复、容量溢出、generation/tick/epoch 不匹配、未来遥测 tick、
  物理 remaining 不匹配均拒收。对象分配地址变化受到测试覆盖。
- 该快照来自独立信念引擎，不从实际比赛复制隐藏手牌、seed 或预约。
- belief-draw CAS 操作在 epoch 切换前 capture，切换后恢复同一物理局面的
  phase 证据；Python 仍检查完整己方观察、FAIR 帧和控制器状态。
- 新增只读观察 scope：原生有效速度 getter 会嵌套调用 movement scale hook，
  旧代码将其误记为真实推进的 step/tick。新版仅屏蔽观察 scope 内的遥测写入，
  getter 返回值及真实推进 hook 保持原样；scope 使用线程局部、可嵌套 RAII。

## 构建和部署

build_phase_snapshot_engine.py 为每次构建创建独立目录，记录输入、编译命令、
生成源码、二进制哈希。使用已有冻结 ABI、native-ID-aware cache 和 compact
capture 路径，不覆盖旧产物。构建结果固定为 training_admissible=false。

phase_snapshot_launcher.py 保留旧服务的资源/基线完整性核验，只为本次
进程选择已核验的新库及库搜索路径；进程记录同时保存新库与启动器身份。
旧服务配置、旧库、历史数据和既有模型均保持原样。

## 本轮验证及不覆盖范围

定向 C++ host 测试覆盖地址变化、重复恢复、ID 重用、身份不符、对象缺失、
重复 ID、容量、未来遥测、无缓存及物理剩余计时不匹配。启动器测试覆盖
正确库选择、基线核验保留、命令契约变化和二进制篡改拒收。

原生目标为两个时间段 × 两个玩家的四个根，每根 A/B/A 三次换牌，每次
两次相同 RNG 续演：12 次换牌、24 次续演。原生 phase 状态须逐字段一致，
不能以 Python 冻结观察或 digest 相同代替。

即使以上通过，也只证明 settled-root 工程子集，不证明 Full128、实际隐藏
状态干预、单方 ready/未结预约、未完成补牌、跨 resident slot 遥测、完整局
吞吐或训练准入。后续继续逐项验收，G0/G1 整体不据此放行。
