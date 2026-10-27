# E2.1-N：Provider 注册、真实资源读取与 catalog 前置阻塞

分支：`codex/pure-linux-libg-headless-20261026`。源基线：
`59d72f70ba6f8dae2990f51272d5296845190d76`，继承原生构造证据 `cb8b10b`。
本报告所在提交用 `git rev-parse HEAD` 查询，不自引用尚未生成的提交 SHA。
完整脱敏命令、逐例退出码及日志 SHA 见
[机器可读证据](pure-linux-provider-catalog-evidence-20261028.json)。

## 结果与边界

本轮确实在 Ubuntu 22.04.5 / Linux 6.8.0-138-generic / aarch64 中运行真实 libg。
JavaVM 未注入，未启动 Android Emulator、ART、Activity 或图形初始化入口。
使用匹配 Android linker/Bionic，不能据此称为 Android-free。

| 对象图节点 | 状态 | 证据与限制 |
|---|---|---|
| libg / constructors / content root-context / filesystem | 历史 PASS | 保留前轮报告；本轮不重复宣称新增覆盖 |
| data provider 注册 | PASS（限定注册） | 原生函数返回，真实 C++ string、namespace、vtable、槽位与释放核验 |
| update provider 注册 | PASS（限定注册） | 独立 update-only 及 both 子进程通过；不代表真实 update 资源优先级通过 |
| 有限资源读取 | PASS | 四个新子进程 wrapper/child 均 exit0，原生文件接口读出的完整字节 SHA 一致 |
| catalog list/preload/分批 load | RUNTIME_OBSERVED | list 158项、listener 368请求；两次批次调用完成，ready 始终0 |
| catalog ready | BLOCKED | 自然 loader-start 进入 Java 日志路径，获取 JNIEnv 时读空 JavaVM 指针而 SIGSEGV |
| ResourceManager | BLOCKED | factory/manager 对象槽建立只是局部观察，未取得 RESOURCE_MANAGER_PASS |
| GameState construct/setup/load | NOT_RUN | catalog 与 manager 前置门未通过 |
| empty/reset/destroy/真实 GameStateStep | NOT_RUN | 未调用 tick getter 冒充推进 |
| 动作、多tick、快照、完整局、Android 对照、吞吐 | NOT_RUN | 保持前置门，不作完整生命周期、no-JavaVM 或10×结论 |

整体 **BLOCKED**；不是 BLOCKED_MISSING_INPUT。当前首个阻塞有可追溯运行证据，
需要闭合原生日志到平台日志的依赖契约，尚非缺少用户 APK。

## 输入、环境与只读保护

- libg SHA：`110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783`。
- 用户 APK SHA：`a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f`。
- runtime/update fingerprint：`e0cb2fb9eb2fdd0df1afcb00df3e3ba61cf25aa8b24e39c17cc391d952a4e339`。
- 8个游戏库、9,829个 assets / 849,482,445 bytes 沿用前轮逐项匹配身份；
  runtime 214/214、update 990/990 再核验。历史 app manifest 为8/9：原锁定整包 APK
  与新输入的整包 SHA 差异继续单列；不更改 supported_engine.json 或旧 gate。
- 185个 ARM64 依赖候选库的静态清单不是本轮实际加载数。逐次 maps 的原始文件私有保存，
  公开证据只列 SHA。libwildcat 不在该185库闭包，不把它伪装成缺失运行库。
- 619个实际暂存 APK 表资源共8,043,281 bytes；backend 索引覆盖9,829条，
  未暂存的已索引资源请求显式失败，不以空数据补齐。
- guest 批次开始时约1.82GB可用磁盘，因此限时、小样本、独立目录，不产生 core 或放量。
- runner 对 payload、probe、资源清单、索引、backend 和观察脚本计算前后 SHA。
  本轮成功例与 loader 故障例输入均未变。主分支本地和 origin/main 均保持
  `0012201830cfbfcbfeb3d9dccd91a855028da869`；冻结任务未接管。

## 实现与参数证据

新增 `tools/pure_linux/provider_probe.c`、`provider_job.py`、`native_assets.c`、
`native_init_attach.py` 及独立测试；扩展原探针和 GDB 观察脚本。

注册不是把普通字符串直接传给原生入口：使用引擎自己的24字节 C++ string 构造契约，
前后 canary、短/长 string 解析、注册后 provider 的路径与 `data:` / `update:` namespace
分别核验。原生注册函数按静态 caller 的两个参数调用，返回契约为 void：
记录返回控制流与对象槽，不把 x0 残留值解释为成功码。
ProviderBootstrap 确认为清理/释放路径使用，不当作构造器。
版本绑定入口由完整 libg SHA、入口16字节 SHA 和运行时入口字节共同守门。

新增只读资源 backend 仅用于新的 Linux host，未修改原 E1 Provider、libg 或其他原始库。
它保留资产真实存在性、长度与压缩标记，read/seek/EOF/descriptor 有独立工程测试；
非空 Android manager、缺失暂存文件、身份不符显式失败。原生测试通过实际绑定位置
确认 AAssetManager_open 指向本实验 backend。**工程测试并非 Android 全语义等价验收。**

DataLoaderFactory 的 packed-reader setter(1)来自该版本真实初始化 caller 的契约，
有 caller/setter/getter 入口守门；并非为绕过 ready 而设置诊断 flag。
factory 注册使用真实 caller 的空根与两字节参数。对象建立、vtable、canary 和
非空 manager 槽可观察；这不证明 manager 完整初始化。注册 factory 和 catalog list
只确认 process-exit 所有权回收，尚无完整析构/无泄漏结论。

strace 的 LD_LIBRARY_PATH/LD_PRELOAD 仅传给 inferior，避免将 Android 库注入宿主工具。
GDB 只 attach launcher 本轮创建并停在 SIGSTOP 的 child；核验模块 SHA，
拒绝与入口 guard 重叠的断点，保留 SIGSEGV，不附加旧进程。
私有观察含线程、参数、返回点及指针槽；公开仅保留计数和因果摘要。

## 真实执行

实际命令脱敏为变量：`$SDK` 是已有 NDK27.3；`$PAYLOAD` 是匹配 linker/libs；
`$RUN` 是新实验目录；`$TABLES` 是619资源根。变量是原路径替换，不代表新运行结果。
JSON 保留每次实际 argv 的路径替换版、wrapper/child/GDB 退出码区别与输出 SHA。

```sh
"$SDK/bin/aarch64-linux-android24-clang" -Wall -Wextra -Werror -O0 -g \
  -Wl,--export-dynamic tools/pure_linux/native_init_probe.c \
  tools/pure_linux/provider_probe.c -ldl -o "$RUN/native-probe-v51"
/usr/bin/python3 "$RUN/public-v51/provider_job.py" \
  --payload "$PAYLOAD" --probe "$RUN/public-v51/native-probe-v51" \
  --output "$RUN/public-v51/result-settings-qualified" --mode data \
  --data-root "$TABLES" --update-root "$TABLES" --data-path / \
  --search-root "$TABLES" --resource-manifest "$RUN/public-v51/resource-manifest-v48.json" \
  --asset-root "$TABLES" --asset-index "$RUN/native-assets-v22.index" \
  --asset-index-sha256 cc7fedcfb2a43a3cc8aae9afb586114915bfc69ae461d92420de349550772916 \
  --asset-backend "$RUN/public-v51/libnative-assets-v47.so" \
  --asset-backend-sha256 905fb58cc33ed308107d433c6c1f9cfd57f6b6e121ff5e77dd1896176e11ea84 \
  --timeout 55 --resource-name data:/settings.csv \
  --expected-resource-sha256 15a460f275b03398f0fd0e7bc86ed2dd651c63e618795cc604a53bc40d2eada4
```

构建 exit0；v51 probe SHA `59e2d28cb1c1b162de8d57a6db3adac61edced3db9ba93d77e0f8cd4e22ba6a0`。
其余三次独立读取是 `data:/version.number`、重复 `data:/settings.csv`、
以及 search provider 的无前缀 `settings.csv`。四次文件对象均核验真实 APK 文件虚表，
逐次 read 总和等于 length，完整输出 SHA 匹配，EOF=0、position=length、at_end=1，
seek(0)=0、position=0、重读首字节一致，原生 close/delete 返回，backend finish=0。
settings 为253字节，version 为252字节；后者 SHA 为
`60aa34c808b7162094bdfd696d724b648bafce2dfe68a99e057c6ad2e716a5ad`。

v49 data-only、update-only、both：各 wrapper0/child0，真实注册和释放完成。
v48 load 与 v49 load-repeat：各 wrapper0/child0，158项 list / capacity224；
preload listener state3、368请求；start=0/39/78/117/156 分批返回0/0/0/0/1，ready一直0。
这只取得 CATALOG_LOAD_OBSERVED，不等于表语义加载完成或 CATALOG_READY_PASS。
loader-construct 的 ctor/dtor/canary 通过，ready仍0。

## mmap 资源证据，避免把零 read 误判成未读取

v48、v49 两个独立进程的 syscall 证据均为：

| 阶段 | 非目录资源 | 只读 MAP_SHARED 字节 | mmap/munmap/close |
|---|---:|---:|---|
| list | 1 | 2,460 | 各1 |
| preload | 368 | 6,069,290 | 各368 |

合计每次369个不同资源、6,071,750映射字节。正 read/pread 系统调用字节为0，
实际走 mmap。fd 与映射在观察窗口闭合，暂存字节 SHA 与 runner 输入一致；
**未对映射内存逐字节 hash，未证明解析内容正确或 ready。**
旧 v54 统计保留，v55 单独补充 mmap 统计，不覆写失败解释。

data/update/search 在这些成功例中指向同一 APK 子集；未使用990个真实 update 文件
构造冲突矩阵。因此真实 update 优先级、fallback、全资源图形/音频语义仍 UNRESOLVED。

## 首个真实阻塞与失败记录

自然 loader-start → native log → Java logger → 获取 JNIEnv → 读取空 JavaVM 全局指针 → SIGSEGV。
native_log 与 jni_env_acquire 在明确 post-load 观察窗口各命中1次；
AAssetManager_open/fromJava 两个已布断点命中0，constructors_covered=false。
这只是限定地址/窗口结果，不证明全路径无 Android 或资源依赖。
**尚未实际调用 AttachCurrentThread**，故障发生在准备读取 VM 虚表阶段。

loader wrapper exit1 / GDB exit1；inferior exit_code=null，最后停止信号 SIGSEGV。
后续 GDB 清理所属 child，不把 wrapper1 写成引擎正常 exit1。
日志分类缓存、lazy 初始化和另一单参数日志入口的限时静态调查，均仍指向 Java logger；
该限定调查没有找到已验证 native sink 注册契约，不等于证明全库不存在此契约。
没有跳过日志、修改 lazy guard、假造 VM 或统一成功返回值。

其他失败原样保留：v49 错误路径 `data:settings.csv` / `data:version.number` child60；
v50 资源打开后，旧探针仅识别 disk 文件虚表而 child61。
v51 添加精确 APK 文件虚表与方法槽核验后才取得四次真实读取通过。
历史 ABI、首次构造和调试失败另见旧报告；本报告未覆写它们。

## 工程验证与发布

- `/opt/anaconda3/bin/python3 -m pytest -q -p no:cacheprovider tools/pure_linux`：exit0，
  108 passed / 1 skipped（最终复核0.96秒）；skip 为本机 macOS 不具备 Linux owned-child 条件，
  真实 guest GDB 路径已实际运行并保留故障证据。
- `/opt/anaconda3/bin/python3 tools/run_main_engine_checks.py`：exit0，211 passed，22.72秒。
- `/opt/anaconda3/bin/python3 tools/run_public_checks.py`：9组 XML 均 failures=errors=0，
  依次794/27skip、23、82、15/3skip、25/4skip、31/3skip、31/2skip、38/3skip、65。
  最终会话结束的 wrapper 退出码未单独持久化，机器证据保留 null；不伪造原始退出码。
- 回归在独立源码副本运行，保留历史 publication XML；源码清单、gitleaks、diff
  的本轮发布检查结果记录在回执和最终提交说明中。测试集合重叠，不累加。

新增代码强调身份与阶段边界，直接复用原生对象而非建立第二套近似 catalog。
工程测试覆盖索引/路径/descriptor/失败显式化和结果验收正负例，不替代原生完整生命周期。
仅提交源码、脱敏摘要和 SHA，不发布资源、二进制、原始地址/汇编/内存或私有日志。

## 下一步唯一前置阻塞

先恢复原生日志服务初始化与 sink 选择的真实契约：从版本匹配 caller 追踪配置、
回调注册、所有权与必要输出语义。若存在原生 sink，先在独立子进程验证完整消息保留与
返回行为，再重新执行自然 loader；若确实只能经 JNI，则明确记录该路径的必需性，
另行设计等价日志桥实验，不通过屏蔽调用推进。
自然 ready 与资源表语义通过后，才验收 ResourceManager，随后进入 GameState。
