# E2.1-N：Linux 数据库所属对象获取入口

基线 `e5e52f9be5fdef8c36bc06c3ca0cbb8823f41068`。本阶段状态
`RUNTIME_OBSERVED_LIMITED`，严格 `CATALOG_READY_PASS=false`。

## 真实实验

在 Ubuntu ARM64 guest 新建隔离目录，使用真实、SHA 绑定的 `libg.so` 和既有
908 项 update 资源、619 项 data 资源及配置副本。独立子进程先执行既有 content、
filesystem 和 provider 设置，再显式调用 Android 侧观察到的获取候选 `0x7557d0`。
这是单独的对象构造实验，不是自然冷启动或 Android 同步轨迹。

首次尝试的 GDB 断点与入口身份保护范围重叠，观察器主动终止，调用尚未执行。
保留该次结果。第二次将断点仅设置在构造候选 `0x755a98` 和数据库初始化候选
`0x75760c`，入口前对象缓存槽为空；构造候选被调用一次，然后进程 SIGSEGV，
未到达获取函数返回，数据库初始化候选在 **post-load 观察窗口内**调用零次。
GDB 停止时 `x0=0`、PC 相对库偏移 `0x17d8b14`，返回地址为 `0x140b320`。
静态指令显示 PC 处通过空全局指针取虚表，调用点周边使用 JNI 风格的虚表调用；
具体调用所属 Java 对象、参数与依赖语义仍未完成运行时验证。

第三次不挂 GDB、使用 strace 的控制进程同样在获取入口后以 `-11` 退出，日志
记录空地址 SIGSEGV；它不是完全无插桩对照，也不证明两次崩溃的完整调用链相同。
三次输入、资源与辅助文件前后身份均未改变。配置写入和 Backup 使用既有隔离
候选后端，不代表 Android Backup 等价；没有伪造 VM、配置或 ready。

## 命令与证据

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -O0 -g -fPIE -pie -pthread -Wl,--export-dynamic \
  tools/pure_linux/native_init_probe.c tools/pure_linux/provider_probe.c \
  tools/pure_linux/preference_store_hook.c tools/pure_linux/preference_store.c \
  tools/pure_linux/preference_files.c tools/pure_linux/preference_contract.c \
  tools/pure_linux/preference_codec.c tools/pure_linux/preference_cipher.c \
  tools/pure_linux/native_backup_queue.c -ldl -o "$PROBE"
# exit 0
python provider_job.py --payload "$PAYLOAD" --probe "$PROBE" --output "$OUT" \
  --mode both --data-root "$DATA" --update-root "$UPDATE" --data-path / \
  --search-root "$UPDATE" --resource-manifest "$MANIFEST" --asset-root "$DATA" \
  --asset-index "$INDEX" --asset-index-sha256 "$INDEX_SHA" \
  --asset-backend "$BACKEND" --asset-backend-sha256 "$BACKEND_SHA" \
  --preference-material "$MATERIAL" --preference-material-sha256 "$MATERIAL_SHA" \
  --preference-xml0 "$XML0" --preference-xml0-sha256 "$XML0_SHA" \
  --preference-xml1 "$XML1" --preference-xml1-sha256 "$XML1_SHA" \
  --native-backup-queue --owner-getter-only --timeout 120 --observe-targets "$TARGETS"
# GDB run: wrapper exit 1，SIGSEGV；control 去掉 --observe-targets，inferior exit -11
python tools/pure_linux/audit_linux_owner_failure.py --debug-dir "$DEBUG" \
  --control-dir "$CONTROL" --output "$AUDIT"
# exit 0
/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux
# exit 0，1078 passed，1 skipped
```

真实调试与控制日志位于 guest `/tmp/nashura-owner-getter-linux-20261029-v1`，
对应结果本机副本位于 `/private/tmp/nashura-linux-owner-getter-20261029-v1`、
`v2`、`v3`；均为私有证据。首次包装器的 exit 文件因 shell 参数转义只含 `n`，
不能作为退出码依据；其运行器自身 result 记录 exit 1 及断点保护错误。
输入/输出 SHA 见[机器证据](pure-linux-owner-getter-evidence-20261029.json)。
运行后为防止未来误标状态，运行器增加成功结果校验；此次实际运行的运行器 SHA
与当前发布版本分别记录，修改仅涉及成功结果判定和字段。

下一步追踪该调用点进入前的真实字符串和调用者，在 Android 基线确认配置读取/日志
语义，再将已验证的对应原生接口接入；不能把空指针直接替换为伪 VM。严格 catalog、
配置异常和 Backup 门未通过，ResourceManager、GameState、Step 均为 `NOT_RUN`。
