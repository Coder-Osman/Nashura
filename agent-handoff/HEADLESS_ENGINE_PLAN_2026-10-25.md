# Headless 引擎执行计划

## 阶段与门槛

1. P0：版本、现有后端、视频证据和冻结任务审计；不修改main及部署。
2. P1：30分钟内在独立目录启动匹配原版ARM64 user-mode进程。自然终局、动作执行与状态一致才算通过。
3. P1通过时，优先交付隔离启动、确定性验证和可复现基准工具；现有sim_core仍为独立研究原型，不因其更快静默替换。P1未通过时再完善8卡MVP。
4. P3：重复动作流、reset隔离、分段推进、实例隔离、动作回执和终局验证。原始内存/私有对局留在私有证据区；只公开摘要与摘要SHA。
5. P4：固定配置/seed/动作流，1/2/4/8个独立诊断进程，每档重复测量。报告端到端完成局、tick、延迟、CPU、内存、启动/reset、轨迹及完整率，不把推进速度等同训练速度。

## 运行隔离

命令必须显式传入私有运行时与resident目录、原库及probe身份、新证据目录。使用空闲loopback端口，不调用已有supervisor的start/stop，不向现有端口发重置命令。只清理本工具创建且保持身份绑定的子进程；文件输出只落新目录。

原生路径复用当前BattleEnvV1/NativeClashEnv契约，不更改训练默认后端、模型、Replay、FAIR边界和单信念配置。工程验证可以读取完整原生审计状态，但该数据不进入策略或训练集。

## 若进入8卡MVP

复用主线sim_core，补reset(seed,match_config)、step、observe(player)、is_terminal、export_trace及显式引擎身份。拒绝越界位置、未知卡/规则，不静默截断。补延迟、手牌轮转、圣水、寻敌/移动/攻击/法术/建筑/死亡/塔/终局的规则覆盖矩阵。固定RNG接口并披露未实现机制。

测试优先内部确定性、reset、同tick、拒绝、分段、实例与可见性；真实原生逐机制/逐边界对照独立标记。没有原生对照只写SIM_INTERNAL_CONSISTENCY_PASS和NATIVE_PARITY_NOT_RUN。

## 发布与训练准入

每阶段在任务分支提交并推送，附命令、退出码、日志SHA和限制；同步README、RESULTS、CHANGELOG及source-manifest。公开检查、主工程检查、专项和凭据扫描分别记录。任何headless或原型验收均不自动改变G0/G1、34/43，不启动G2–G8或正式训练。
