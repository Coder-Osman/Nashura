# 阶段性同步流程

用户已要求每完成一个目标更新本公开仓库。维护时使用现有 GitHub 账号 Coder-Osman，目标仓库为 https://github.com/Coder-Osman/Nashura 。在本仓库提交、推送，保留其他工作区及运行中冻结部署。

1. 完成该目标并取得对应验证结果；更新进度、成果、局限和 CHANGELOG。
2. 同步前执行 `git fetch origin --prune`，检查所有远端分支，识别实际最新提交。先保留本机未提交成果，再整合所选版本；核对远端新增源码与仓库外源工作区，完成逐文件同步后再导出，防止旧源目录覆盖刚拉取的新实现。运行中的冻结部署独立保留。

从源工作区导出必要文件：

```sh
python tools/export_public_snapshot.py --workspace /path/to/CR --destination /path/to/Nashura
```

3. 检查新增、修改及失效文件；导出器只新增/覆盖，不自行删除旧文件。新成果若在嵌套目录，先扩充显式选择规则。除已登记的旧密码默认值移除外，发现真实凭据立即停止发布并修正副本。
4. 运行源码清单校验、公开测试和凭据扫描：

```sh
python tools/check_public_snapshot.py
python tools/run_public_checks.py
python tools/run_main_engine_checks.py
gitleaks dir . --config .gitleaks.toml --redact
git diff --check
git diff --stat
```

5. 审阅后显式暂存本次发布文件，再运行 `git diff --cached --check`，使新增文件也进入检查。检查与提交分开执行；任何失败先修复，再提交与推送。历史失败报告若需保留原始字节，只对该报告登记精确的 Git whitespace 属性并解释原因，不批量豁免源码。
6. 仅提交审阅后的阶段成果，使用描述目标的提交消息，然后 `git push origin main`。核对远端 HEAD，汇报提交与测试结果。

当前主工程检查和冻结 history-v2 检查分别记录；两者均为 CPU 工程测试，不代替新实现的真实 CUDA 数值对照、batch256 跨进程恢复或原生完整搜索局。

`tests-batched.xml` 保留早期失败回溯的原始尾随空白，`.gitattributes` 仅为该文件关闭行尾空白提示；其内容仍受源码清单 SHA 校验。迁移 KL 发布后的补充提交修正了原流程未检查新增文件的问题，未改写历史证据或提交。

初次发布仅包含源码与历史证据摘要。之后每次完成目标同步，不将每条进度消息转成提交，不创建重复定时任务。

combat-snapshot-v1/tests-initial.xml保留首次fixture构造错误的原始回溯和尾随空白；
仅该精确路径关闭行尾空白提示，仍受源码清单SHA校验。修正后的测试与原生验收
另行记录，不重写原始失败报告。
