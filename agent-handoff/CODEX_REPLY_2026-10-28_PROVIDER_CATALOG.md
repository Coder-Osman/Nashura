# Codex 回执：E2.1-N Provider / catalog

任务分支：`codex/pure-linux-libg-headless-20261026`。
基线：`59d72f70ba6f8dae2990f51272d5296845190d76`。
实现、报告和回执同一提交；提交 SHA 用 `git rev-parse HEAD` 查询，推送后核对远端。

## 已执行

- **IMPLEMENTED / UNIT_PASS**：版本绑定 Provider/catalog 探针、只读 native asset backend、
  owned-child GDB launcher、严格资源结果验收；108项通过、1项平台条件跳过，exit0。
- **PASS（限定注册）**：data-only / update-only / both 三个原生子进程注册、namespace、
  string canary、vtable 和释放通过，各 wrapper0/child0。
- **PASS（有限读取）**：四个真实原生文件读取子进程 wrapper0/child0，settings/version
  完整字节 SHA、read/length/EOF/seek/重读/close 和 backend finish 通过。
- **RUNTIME_OBSERVED**：两次 catalog list158/preload368/分批调用返回；每次369个资源
  mmap共6,071,750字节。暂存资源SHA核验，映射内存未hash，ready仍0。
- **BLOCKED**：自然 loader 的日志路径在取得 JNIEnv 时读取空 JavaVM 指针而 SIGSEGV。
  wrapper/GDB为1，inferior exit_code=null；未实际调用 AttachCurrentThread。
- **NOT_RUN**：GameState、empty/reset、真实Step、动作、完整局、Android对照、10×与训练。

## 验证与证据

完整命令、输入/输出SHA及逐例退出码见[报告](PURE_LINUX_PROVIDER_CATALOG_2026-10-28.md)
和[JSON](pure-linux-provider-catalog-evidence-20261028.json)。环境为真实Ubuntu22.04.5/aarch64，
匹配Android linker/Bionic；主工程211项通过exit0；公开回归9组XML零失败零错误。
公开入口最终wrapper退出码未单独留存，保持null，不与XML通过混写。
公开回归与主工程在隔离副本运行，历史XML不覆盖；各组范围不相加。

本轮发布检查：`/opt/anaconda3/bin/python3 tools/check_public_snapshot.py` exit0，
2,373个文件SHA/长度核验；`/opt/homebrew/bin/gitleaks dir . --config .gitleaks.toml --redact`
exit0、0 findings；`git diff --check` exit0。专项最终复核命令为
`/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux`，
exit0、108 passed / 1 skipped，0.96秒。

库/资源/probe/辅助输入前后SHA一致，历史整包APK差异保留；真实update优先级与fallback
尚未验证。factory/list仅process-exit所有权回收，不声明完整manager析构无泄漏。
首轮错误路径child60及旧probe虚表分类child61和原始调试失败保留。

## 尚待解决

当前首个阻塞是日志服务原生输出契约，catalog ready=0；不是缺少用户输入。
下一步先恢复真实sink选择/注册与消息语义，以独立子进程验证，保持原初始化顺序。
catalog与ResourceManager门闭合前不调用GameState；不以跳日志、假VM或默认值推进。
没有取得无JavaVM冷启动、Android-free或真实完整战斗的验收结论。

main/origin/main、冻结任务、原始APK/libg/旧Provider/update均保持原样。
阶段发布限定本任务分支；仅源码及脱敏证据，不上传私有原始日志或二进制。
