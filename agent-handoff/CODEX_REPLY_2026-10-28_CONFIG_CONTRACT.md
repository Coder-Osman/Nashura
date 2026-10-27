# Codex回执：E2.1-N配置候选租约与初始化契约

工作分支：`codex/pure-linux-libg-headless-20261026`。
基线提交：`237a448d824a5ab728b861f2993dd1d13932a5fb`。
本回执的发布提交由该分支Git历史记录，避免在文件内自引用提交SHA。
整体状态：**BLOCKED**，尚未取得严格CATALOG_READY_PASS。

## 本轮交付

| 项目 | 状态 | 实际结果与边界 |
|---|---|---|
| 配置候选租约 | IMPLEMENTED / UNIT_PASS | 生命周期flock、线程互斥、已初始化上下文fork拒绝；不等于Android配置并发语义 |
| 两次ARM64/Bionic候选实验 | RUNTIME_OBSERVED / PASS（限定候选） | v81/v82各11进程回执符合预期，187项输入前后SHA一致；未加载libg |
| DEX方法审计 | IMPLEMENTED / STATIC_FOUND | 绑定DEX及dexdump SHA，保留寄存器/异常表，数组字节单独校验 |
| 配置方法身份 | STATIC_FOUND | 8/8限定方法摘要相同，不表示传递调用和平台实现等价 |
| 完整初始化 | UNRESOLVED | 含onCreate时8/9相同，实际审计exit2；配置对象前缀仍有条件分支目标差异 |
| 配置语义 | UNRESOLVED | AES/legacy、损坏清理、独立commit及失败中间状态、backup副作用仍未闭合 |
| 真实catalog | BLOCKED | 保留历史自然ready证据；本轮没有重跑真实catalog |
| ResourceManager | BLOCKED | 严格前置门未通过 |
| GameState/空场/reset/Step/动作/完整局 | NOT_RUN | 未越过前置门 |
| Android逐tick对照/无VM生命周期/10倍/训练 | NOT_RUN | 不以候选实验或工程测试替代 |

## 实际命令与证据

完整命令中的私有路径仅保留于本机隔离审计。下列变量是对应真实路径的脱敏映射；
输入SHA、运行状态和输出SHA见[机器证据](pure-linux-config-contract-evidence-20261028.json)。
原始DEX、二进制、反汇编、运行路径及日志正文没有纳入公开仓库。

```sh
aarch64-linux-android30-clang -Wall -Wextra -Werror -fPIE -pie -pthread \
  tools/pure_linux/loading_config.c tools/pure_linux/loading_config_probe.c -o "$PROBE"
# exit0；probe SHA f57def5a979c312d9322994e596e51415db291d00f75e7adb1dce4323e2195e9

python3 "$EXPERIMENT/config_lease_job.py" \
  --probe "$EXPERIMENT/probe" \
  --probe-sha256 f57def5a979c312d9322994e596e51415db291d00f75e7adb1dce4323e2195e9 \
  --linker "$PAYLOAD/linker64" \
  --linker-sha256 e6a2026a6252e0a80c242555e147faca98cd557e2adac1927cd5c096d70ce4c7 \
  --library-path "$PAYLOAD/libs" --output "$EXPERIMENT/result"
# v81、v82各exit0；隔离目录互不覆盖

python3 tools/pure_linux/preference_method_audit.py \
  --dexdump "$DEXDUMP" \
  --dexdump-sha256 9c4addfdb5373f0923e418a64683976922c1ffdd1a3e9673b9cce5a0838e9692 \
  --left "$CURRENT_DEX" \
  --left-sha256 c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6 \
  --right "$HISTORICAL_DEX" \
  --right-sha256 ef1ac708f6e836daa925afc50d3bf3b139213b3a72323a354cc2e9493b50f608 \
  --include-initializer --output "$AUDIT"
# v85 exit2，初始化差异保留

python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit0：181 passed，1个平台条件skip
```

真实候选运行环境是Ubuntu22.04.5、aarch64、Linux6.8.0-138-generic及匹配Bionic。
两次结果SHA分别为
`4d9d0f7d157760a975e1ca79ede548f762a9e697ee0135ba39e1dea007cdc2e2`、
`9d3b880aa91480543532557d350e805b916d90a8d6af3f636c01a3e27a36b858`。
v85方法审计SHA为
`6f211bff1a2026c522adfa9f02571e25565748befadcc57453dc7a09f8d85924`。

## 工程回归与失败保留

首次v86使用系统Python3.9，主工程及公开检查wrapper均exit1，内层pytest均exit2：
分别8项、20项收集错误，原因为已有代码使用`dataclass(slots=True)`。
原始stdout/stderr和退出码保留；没有为此改写无关源码或历史XML。

改用已安装Python3.13.5，未安装或升级依赖。在新的v87源码副本运行：

```sh
python3.13 tools/run_main_engine_checks.py
python3.13 tools/run_public_checks.py
```

主工程exit0，211 passed；公开检查exit0，九组共1,104 passed、42 skipped，零failure/error。
gitleaks与`git diff --check`均exit0；日志SHA及九份新XML的SHA见机器证据。
`python3 tools/check_public_snapshot.py` exit0，核验2,393项源码。
暂存后另执行`git diff --cached --check`，通过后才提交。
所有新测试XML留在隔离副本，公开工作树历史XML保持原字节。
这些是工程回归，不构成真实引擎配置等价或完整局证据。

## 历史保护与下一步

main与origin/main保持`0012201830cfbfcbfeb3d9dccd91a855028da869`。
三份历史provider/catalog报告的SHA保持不变；没有启动模拟器、训练、放量或性能实验。
公开提交仅含本轮源码、脱敏报告及源清单。

下一步的前置阻塞是**配置初始化与持久化/backup契约**：先闭合条件分支目标和传递依赖，
再恢复编码、分步commit/failure及backup调用契约，然后在两个新子进程重验真实catalog。
这不是缺APK或guest；保持ResourceManager、GameState与真实Step的前置门，不以默认值放行。
