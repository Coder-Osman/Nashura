# daybreak / clapha-engine 交叉核验（2026-10-27）

## 结论与边界

**BLOCKED_EXTERNAL_DEPENDENCY**。在当前可访问搜索范围内未定位外部 clapha-engine；
当前账号访问其仓库和 release 均为 HTTP 404。已完成只读取证及声明/实物版本映射，
尚未执行 Python 导入、最小原生运行或固定轨迹比较。Android-free、JavaVM/JNI 依赖与
实际引擎兼容性均为 unknown。404 不证明项目不存在。

按用户要求暂停 E2.1-K；其 13 个未提交文件保留且不纳入本次发布。
未附加、停止或重启既有实例；本轮没有新的引擎运行。
main/origin/main 保持 0012201830cfbfcbfeb3d9dccd91a855028da869。
工作分支 codex/headless-engine-plan-20261025，
起点 dba6d81bf8ea58413f83123d0a108955891e0f6b。

执行依据为用户附件 DAYBREAK_CLAPHA_CROSSCHECK_PROMPT.md，
SHA256 eed1d984f42af5b884028aa18c06e11a7730095fee6fd79603ac9bf7e22585d8。
本报告不发布外部二进制、资源、原始审计或第三方源码副本。

## A：依赖寻找（只读观测）

宿主 Darwin / arm64 / macOS 15.6。搜索用户主目录、/workspace、/opt 内的
clapha-engine 目录、额外 daybreak-cr 目录、engines-*.tar.zst、data-*.tar.zst 和
有执行位且名称为 engine 的文件，三轮 stdout 均为空。组合 shell exit 1；
各 find 退出码未单独捕获，JSON 保留 null，未冒用组合退出码。

每轮 stderr 125 行：124 行权限拒绝及 /workspace 不存在。三轮范围重叠，
不将其相加当作独立受限目录数；受限位置和改名目录仍 unknown。
未改变系统权限，未进行系统安装。

GitHub API 固定 daybreak：
[Jason-XII/daybreak-cr@73334a00](https://github.com/Jason-XII/daybreak-cr/tree/73334a00ee38bab949c5bc88dc133cad42ffd4f4)。
递归树 103 项、91 blobs，truncated=false。只读取 12 份公开文本；
16 条收集命令中 14 条 exit 0、2 条 exit 1（clapha 仓库与 release HTTP 404）。
未下载 prebuilt/libcrprobe.so，也未执行外部仓库代码。
命令、各文件 SHA 和字节数见 inventory；原始响应和 stderr 留在私有证据目录。

## B–D：加载契约（静态发现，非运行验证）

cr/engine.py:155 起将外部 crx 加入 sys.path，导入 loader、env.CrxEnv，
构造 CrxEnv(clapha/'engine', workdir, clapha/'data'/'assets')。
reset 调用 create_match、reader.logic、step 和 frame/objects 观测。
loader 的 sysconf/get_nprocs shim 使用 Bionic CPU 编号；外部实现未取得，
这只证明 wrapper 期望特定加载层，不证明 VM/JNI 已消失。

convert.py:1009 把 --engine 参数声明为 NAME/LIBDIR/ASSETS；
因此 engine 可能是库构建目录，实际文件类型、架构、ABI 未知，
不能预先把它限定为单一 ELF 可执行文件。README/arena/ppo 也要求外部路径。

tests/test_engine.py 明写 no native engine，并用 Engine.__new__ 构造 wrapper fixture；
其单测与 README 的无引擎测试说明均不是原生运行证据。
convert.py:897 用 os._exit(0)，注释称 exit-time destructors crash；
这是上游静态生命周期风险声明，本轮未复现，也未验证正常 destroy。

docs/CONVERSION.md 声明 clapha >= ba78ae1、conversion-2026-10-24 release，
并明确列出 engines-*.tar.zst 与 data-*.tar.zst。实际 release 清单/资产/校验和
尚未取得。文档还涉及 150535029 与 160402002/012/020 构建及不同时间资源；
旧客户端 manifest 不代表实际选用的转换引擎版本。

缺少完整 bundle，C 导入和 D 最小运行均未执行。
minimal_run 中退出码、PID、线程、耗时、资源访问及运行结果均为 null/unknown。
未在 Darwin 执行 Linux ELF；未安装 Linux/QEMU 或启动新的 Android 实例。

## E：版本映射

| 项目 | daybreak 声明 | Nashura | 本轮结论 |
|---|---|---|---|
| APK SHA | 685c115e…bcf2a | supported manifest 相同；实际实验包 f1ee6d61…7dd9b | 声明相同、与实际包不同 |
| libg SHA | 110aa2b5…59783 | 本轮重算为同一完整 SHA | 声明与本地实物相同 |
| ABI / 资源目录 / fingerprint | arm64-v8a / nr_15.535.13_release_ff1c6c29 / e0cb2fb9…4e339 | 清单相同 | 仅声明一致 |
| 实际 clapha 引擎与资源 | 未取得 | 当前原版输入留存 | unknown |
| 导出、DT_NEEDED、入口/ABI、Provider、配置桥、tick | 未取得 | 不单方面推断对应关系 | unknown |

完整 SHA、比较范围和证据等级见 version_matrix。
本轮 file/hash 确认 Nashura libg 为 AArch64 ELF shared object，
BuildID 90e6f351f0dec4a28b494c3812d2629fd45d5a83。
历史 E2.1-K 输入记录含 probe/fmod/APK index/990 update 摘要；本轮未重采 guest，
不得将历史清单标为新的运行时观测。不同序列化的 update 清单摘要不可直接比较。

daybreak 的可选 live_review_patch 另声明 libg SHA f7d55ed3…fb63，
实际外部引擎是否使用未知；本轮未应用补丁。
**同一 libg 声明 SHA 不足以证明完整资源、初始化、配置或快照兼容。**

## F：固定轨迹

未执行。没有新生成的 3602 快照、28 动作、tick3601 终局或状态 hash。
不生成 trajectory_comparison.json 来模拟已运行结果，
历史 Nashura 轨迹仅作为日后对照输入。

## 首个未解决依赖与下一步

首个缺口是可定位、合法准备且完整的 clapha bundle：
crx 源码、engine 构建/库、data/assets、完整版本指纹、资源逐文件 SHA、构建/运行方式。
获得后先 file/hash 及读取 loader/env，确认真实加载格式与运行宿主，
不要在 Darwin 直接执行 Linux ELF。

按 A–E 顺序先验证导入和最小生命周期，再核对实际 libg/资源/入口。
仅最小运行成功且身份可对应时，定义固定轨迹字段映射；
序列化相同才比较 3602 快照字节，否则报告字段差异。
本轮不进入 E2.2/no-VM、Android-free 终验、正式训练或 10× 实验。

## 证据与检查

- inventory：搜索边界、公开源码身份及实际收集命令/退出码。
- version_matrix：声明比较与实物比较分开，缺失侧 unknown。
- minimal_run：未执行，未以 0/false 伪造动态依赖计数。
- failure_index：404、受限搜索和辅助路径错误保留；不是引擎失败判定。
- 发布检查及回执见 CODEX_REPLY_2026-10-27_DAYBREAK_CLAPHA.md。
