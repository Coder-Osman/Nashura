# 全局随机状态的原生快照覆盖诊断

2026-10-23，独立attempt `native-global-rng-1792732935729300000`在.35端口29779
顺序启动两个全新引擎。Full128、开局先验采样及terminal选优保持原冻结部署。

## 已取得的证据

固定libg初始化数组slot `0x18e5370`注册入口`0x1335848`。该入口保存真实LR，
BL到`0x1a1cb0c`后恢复LR，再顺序进入已绑定的time(NULL)播种路径，初始化
全局MT对象`0x19fc368`。这补齐此前alias审计的加载注册入口缺口；三段消费者
代码传该地址到MT generate，但静态绑定不证明固定速猪战斗实际调用消费者。

两个原生进程分别在合法WAIT轨迹tick90/1200/3600/5400检查2504字节对象。
每个边界建快照后仅将对象首字节异或1，扰动期间零游戏推进、零决策，再恢复：

- 八次均`retained_perturbation`：快照恢复保留扰动，未恢复这块全局内存。
- 八次扰动前后`state-digest`相同：该摘要未检测这块全局对象变化，不能作为
  所有随机状态已恢复的证明。
- 每次均在推进前CAS修回原字节，恢复原摘要、释放快照；generation/stateEpoch/tick
  过期身份均拒收，所属进程和端口清理通过。独立验收核对18份归档。
- 两进程各自沿WAIT轨迹全局字节保持启动值；两个进程启动字节摘要不同。
  后者与time播种代码相容，但不独立证明具体时钟值，更不证明战斗结果受墙钟影响。

两段探针耗时85.610秒与88.767秒，不是Full128搜索或对局耗时。
验收状态`verified_diagnostic_only`只表示上述诊断结果及证据链已核验，不表示
随机状态覆盖通过。原始进程内存hex只留私有归档，公开仅发代码和摘要。

## 固定身份与测试

- launch：`d271ae1d3e90734a04ff11062b1a50c7b89fba1a0952ae0e0cbd3dce25f3bdf2`
- download receipt：`625b12c8b24c08720adadcba1f442aea6b03056557188d3cdddfa4e3fb7daa0f`
- verification：`503166857e322b81dcb55e335e5e239774ca89791755df116292a18c3ee5f2ee`
- static report：`3ab36facb35441f5acee0b956ae20a254381e67a2355e1efad911d78d9aecc4a`
- 最终联合XML：`5ba869f53a73a048b1210d8d0029d067d415cd3c10395f79632ac9f5e3c702e9`

新增33项包含只读/CAS、active及过期身份拒收、结果逐字节分类、固定库重算、
整包验收及重新计算回执后的篡改拒收；包括清理泄漏、复用相同进程、源码/轨迹/
来源替换和外部身份错误。与既有alias/shuffle/seed联合138项通过、6.06秒，
零跳过；集合重叠不相加。公开副本缺私有libg时对应静态重算跳过。
早期tests-initial.xml与tests-final.xml保留各次执行记录，最终实现以
tests-combined.xml为准。公开工程测试不替代本次私有原生证据重验。

## 尚待完成

固定速猪实际使用路径、其它随机组件及间接调用、全局状态完整快照方案、
一般原生pending/fallback、完整搜索终局与恢复仍待验。后续先对实际消费者
取证，在独立新版修复覆盖并复验，不热改当前冻结部署。
G0/G1及34/43登记保持开放，G2–G8未启动，training_admissible、gates_closed
和hidden_random_coverage_closed均为false。
