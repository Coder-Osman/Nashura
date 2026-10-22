# 复现层次

## 公开源码测试

从仓库根目录安装 requirements-test.txt 后运行：

```sh
python tools/check_public_snapshot.py
python tools/run_public_checks.py
```

默认使用 history-v2/project 的冻结 Python 模块运行协议、搜索、时钟、恢复及一般路径内存测试；不访问远端机器，不加载私有模型或引擎。测试输出保存在 publication/checks-public.xml（本机产物不提交）。

主工程完整依赖参见 firstlight_CR/requirements.txt。历史主工程的 UI、PPO、原生测试各有外部依赖，默认公开测试仅选择明确独立的工程范围。

## 原生运行与审计

原生对局需要匹配引擎、资源与 checkpoint，相关身份见冻结规范、各版本 CONTRACT.md、version-manifest.json 和源码中的校验。

原生命令/队列审计预期 libg.so 的 SHA-256 为：

`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`

自行准备文件后放入：

`_spec_work/versions/fair-final-history-v2/project/artifacts/standalone-engine-20260926/app-libs/libg.so`

再运行：

```sh
python -m pytest -q \
  _spec_work/versions/fair-final-native-command-audit-v1/test_audit_ct86.py \
  _spec_work/versions/fair-final-native-queue-audit-v1/test_audit_queue.py
```

这些测试不包含在默认公开测试中，缺少二进制时不得视为通过。

远端调度脚本沿用工作区 MACHINES.md 接口及原机器路径，需自行在本机配置；仓库不包含真实配置。运行前核对资源、端口、源码身份与阶段门。禁止直接用旧实验入口启动正式训练。
