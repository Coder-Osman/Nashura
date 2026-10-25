# 原生动作执行链的独立动态观察

本版复用plugin-runtime-v1已验证的独立QEMU10.0.13、只读寄存器插件、
进程就绪后ARM、监督封存与三组完整轨迹对照，不修改冻结运行中的任务。
新增七个事前指定指令点：ct86工厂分支、构造函数、执行器入口与返回、
位置helper入口与返回、执行器内helper调用后的首条指令。

插件仍只读取PC/X0/X1/X30，不读取或写入游戏内存。原八个随机入口保留作
reset正向对照。C源码从固定父源码以计数受检的变换生成，全部生成字节随
launch和下载回执绑定。日志格式沿用v3，目标集合扩至十五个并明确修改范围
描述；原版本源文件和原始失败证据保持不变。

独立验收先执行父验收器的全部内容检查：三个不同进程的完整终局、全部
FAIR/mask/动作/执行回执/结算时钟一致、周期原生JSON观察一致、构建与源码
身份、连续日志序号、ARM实际base、正向对照及所属进程清理。
再逐vCPU配对执行器与helper入口/返回，核对LR和helper返回后指令的结果。
构造对象按进程内地址关联；不公开原始指针。返回码保留完整分布，不给未知
值补造语义；有限合法脚本的成功回执不证明全部合法动作的到期充分性。

同一helper可能由其它调用者使用，此时只检查函数调用配对，不归于ct86。
观察到ct86执行器进入及返回、helper返回至0xd30ccc，才作为本轨迹的真实
执行链证据。构造函数和工厂命中分开报告，不凭静态映射假设实际命中。
尚未观察到的拒绝分支、一般pending/fallback、全部随机机制及完整Full128
搜索局/恢复不随本专项通过；G0/G1与34/43计数保持原状态。

首次十五指令点尝试在插件组资源表预加载阶段触发原service的140秒启动截止，
尚未就绪，失败归档保持原字节。独立后续尝试使用父trace_launcher已有的
计数受检service_source变换，将三组service启动截止统一为600秒，外层等待
660秒。只改变启动等待，不改guest参数、原生规则、probe预算、完整轨迹比较
或清理要求。两个新源码变换均核对父源码形状，上传身份逐字节绑定。

第二次尝试在600秒service截止之前被SIGALRM终止，仍未就绪，父验收拒收。
新增目标在未知实际base时也匹配其它模块的页内偏移，原回调每次打开ARM文件。
后续生成字节在base未知时只允许原八个随机目标的页内偏移检查ARM；新增七点
在此阶段直接返回。原八点首次观察到ARM后固定实际base，全部十五点恢复同一
精确地址过滤与寄存器采样。此修复不声称记录ARM被首次读到前的新目标；本专项
只判断完成reset正向对照后的战斗期调用。两次失败原归档保持，第三次三组
完整对照已通过限定验收，见EVIDENCE.md及native-evidence-summary.json。
未修改guest或关闭原生alarm；单次成功不作为独立的开销因果估计。

## 独立运行与归档重验

从工作区根目录在新Python进程调用run_live.py；不要在父八目标专项所在的
pytest进程中导入并configure本模块。两版复用plugin_protocol模块名，目标集合
属于进程内状态。公开检查入口因此为本版创建独立pytest子进程。

```sh
python "_spec_work/versions/fair-final-live-command-v1/run_live.py" launch \
  --build "$BUILD_ATTEMPT" --build-launch-sha "$BUILD_LAUNCH_SHA" \
  --ancestor "$TRACE_ANCESTOR" --ancestor-launch-sha "$TRACE_ANCESTOR_SHA"
python "_spec_work/versions/fair-final-live-command-v1/run_live.py" collect \
  "$ATTEMPT" --launch-sha "$LAUNCH_SHA"
python "_spec_work/versions/fair-final-live-command-v1/run_live.py" verify \
  "$ATTEMPT" --launch-sha "$LAUNCH_SHA" --receipt-sha "$RECEIPT_SHA" \
  --ancestor "$TRACE_ANCESTOR" --build "$BUILD_ATTEMPT" \
  --snapshot-parent "$SNAPSHOT_PARENT" --library "$LIBRARY" \
  --output "$ATTEMPT/verification.json"
```

参数指向经身份核验的父归档与固定SHA库。collect在worker仍运行时仅写轮询
记录，退出后收取归档并输出receipt身份；运行中状态不视为成功。verify输出
仅在父验收和真实调用链核验均通过后写入。test_native_archive.py读取真实私有
归档时重新计算，不加载预制成功摘要；公开副本缺附件时显式跳过，对工程夹具
的通过与实际归档通过分别报告。原始地址、寄存器和第三方库不随公开副本发布。
