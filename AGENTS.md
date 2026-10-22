# Hashura 工程维护规则

使用简体中文沟通。用户已明确要求：每完成一个目标，将必要工程代码、文档与成果同步到公开仓库 https://github.com/gujiga/Hashura 。该指令授权本仓库的阶段性提交与推送；同步不修改运行中的冻结部署。

1. 完成目标后更新 README 的阶段状态、docs/RESULTS.md 和 CHANGELOG.md；区分工程测试、限定子项、整体验收门与实战强度。
2. 将必要源文件及证据摘要纳入发布快照。禁止提交 MACHINES.md、真实 .env、密钥、密码、令牌、原始私有审计、模型权重、第三方引擎和提取资源。
3. 源工作区位于仓库外时，用 tools/export_public_snapshot.py 显式导出。检查 git diff --stat 与删除项；导出器不自动删除旧文件。
4. 原始证据摘要保持历史含义；公开副本与源文件存在差异时，在 publication/source-manifest.json 记录原始 SHA、公开 SHA 和变换原因。缺少外部附件的证据标为历史记录，不声称可完整重验。
5. 运行 tools/check_public_snapshot.py、tools/run_public_checks.py 和 gitleaks。修复检查失败，记录结果后再提交；只提交本仓库已审核的内容。
6. 使用目标导向提交说明，推送 origin main；禁止强推和重写历史。检查远端 HEAD 与本地提交一致，再汇报仓库链接和验证结果。
7. 不降低验收门、不用 fixture 替代原生完整局、不把 CE 降低解释为实战提升。失败及未完成状态如实保留。
