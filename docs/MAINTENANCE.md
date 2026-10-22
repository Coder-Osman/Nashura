# 阶段性同步流程

用户已要求每完成一个目标更新本公开仓库。维护时使用现有 GitHub 账号 gujiga，在本仓库提交、推送，保留其他工作区及运行中冻结部署。

1. 完成该目标并取得对应验证结果；更新进度、成果、局限和 CHANGELOG。
2. 从源工作区导出必要文件：

```sh
python tools/export_public_snapshot.py --workspace /path/to/CR --destination /path/to/Hashura
```

3. 检查新增、修改及失效文件；导出器只新增/覆盖，不自行删除旧文件。新成果若在嵌套目录，先扩充显式选择规则。除已登记的旧密码默认值移除外，发现真实凭据立即停止发布并修正副本。
4. 运行源码清单校验、公开测试和凭据扫描：

```sh
python tools/check_public_snapshot.py
python tools/run_public_checks.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
git diff --stat
```

5. 仅提交审阅后的阶段成果，使用描述目标的提交消息，然后 `git push origin main`。核对远端 HEAD，汇报提交与测试结果。

初次发布仅包含源码与历史证据摘要。之后每次完成目标同步，不将每条进度消息转成提交，不创建重复定时任务。
