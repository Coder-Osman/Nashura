# E2.1-B 阶段回执

**限定证据推进已完成；E2.1-B 整体未闭合，G-E2.1 仍 BLOCKED。**

- 基线：`5643fb30ebee184b5f990ae5cd9aab14a02c12bb`。
- 分支：`codex/headless-engine-plan-20261025`；仅发布此分支。
- main/origin/main：`0012201830cfbfcbfeb3d9dccd91a855028da869`，保持原样。
- 交付 commit 为包含本回执的阶段提交，最终消息与远端核验提供实际 SHA。

## 实际结果

- 五个独立原生进程各 3602 快照/28 动作/tick3601 终局一致，snapshot/restore/reset 一致。
- 匹配 APK 的 DEX 定位双 store、Cipher、同步 commit、按变化写及 backup 路径；
  原桥显式 Java 窗口观察变化/空值/恢复各两次 commit，同值零次。未取得 commit boolean。
- fresh-process 自然初始化读回受控值并加一通过；按 wrapper 值层和 replacement 路由层
  分别核验，不拼接两个 observer 的 sequence。
- 双线程同值探针共 8 写/8 读，6 对 Java root 真正重叠；commit0，保存值保持。
  不证明不同值竞争或一般线程安全。
- 46 键均保留原路由，必需性/可移除性全部 unknown；未新增 replacement。
  本轮 fallback110=99 自然＋11 probe，和旧115–116的差异来自探针集合。
- E1 平台资源事件0、Provider failures0/live0继续通过限定检查；JNI 参数采样非穷尽，
  两个历史短 stub hook 仍未覆盖。

## 测试与门禁

- `tools/verify_e2_1b_evidence.py`：真实私有附件 21,861 项，CLI exit0，
  仅 `bounded_evidence_verified`，strict_gate=BLOCKED。
- `tools/verify_e2_1_gate.py`：旧 E2.1 私有 manifest 重验实际 exit2，严格门保持 BLOCKED。
- 四组新增专项：65 passed/7.99s，exit0，包含真实附件与负向突变；
  脱敏器首次1 failed/64 passed保留，修正后通过。
- 主工程211 passed/41.29s；公开主组794 passed/27 skipped及八专项全部 exit0。
  集合重叠，不相加；公开测试不是无VM验收。
- 两份 JS 语法检查和 ARM64 helper `-Wall -Wextra -Werror` 编译 exit0，
  复编 helper SHA 与实测一致。
- 脱敏导出器重新验证真实附件，白名单导出两份 JSON；原始值/资源/审计不公开。

命令、失败及边界详见 `E2_1B_REMOVABILITY_PN_SEMANTICS_2026-10-27.md`。
发布检查实际结果：`tools/check_public_snapshot.py` exit0，核验 2,449 个文件；
`gitleaks dir . --config .gitleaks.toml --redact` exit0，扫描约33.90MB，未发现泄露；
`git diff --check` 和暂存后覆盖新增文件的 `git diff --cached --check` 均 exit0。
仅提交任务分支；实际提交 SHA 和远端一致性在交付消息列出。

## 未解决依赖和下一步

PN 的 commit boolean/写失败/不同值竞争、配置 close/free、Java 引用退休、
重复配置构造 ABI，以及46键消费者和移除反事实仍待验证。
下一条实验先只读观察双 store 身份和 commit 返回，再在独占进程验证受控不同值交错。
不以统一默认值替代未知语义，不进入 E2.2/no-VM 或 Android-free。

本轮独占实例全部回收、监听器残留0，原有 adb 身份未变。main、冻结任务、
原始输入和训练后端保持原样；未启动训练、G0/G1 放量或10×实验。
