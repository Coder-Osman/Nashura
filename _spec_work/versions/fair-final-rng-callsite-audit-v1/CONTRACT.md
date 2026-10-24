# MT初始化参数的局部静态审计

目标为G0/G1清单中尚未解释的直接初始化引用补充参数来源，保持原生运行包原样。
核对固定libg的完整SHA，并实际重算shuffle-audit父证据的18处直接B/BL及EH范围。
每处反汇编一个完整、连续、最终调用MT初始化的局部片段，输出X0对象和W1种子
的表达式。不把EH范围当成唯一函数入口，不跨控制转移拼接参数来源。

仅解释MOV、无移位ADD/SUB与立即数。其余寄存器写入显式标为opaque_write；
片段入口为live_in。W寄存器写入保留零扩展、读取保留低32位语义，避免把
X/W别名当成两个独立寄存器。直接调用使X0–X18失效，X0仅标为该调用的返回值；
保留X19–X29与SP，假定被调代码正常返回且遵守AAPCS64。X18按失效处理，
不依赖平台保留。调用体语义、指针逃逸及内存内容均不从返回标记推断。

ABI依据为Arm官方abi-aa/aapcs64/aapcs64.rst §6.1.1与§6.5：
`https://github.com/ARM-software/abi-aa/blob/7485cc1a1240d9143c6c759390469ae0cbe2d02e/aapcs64/aapcs64.rst`。
实施时从官方API读取该提交的原文，blob SHA为20f9efbbde6f2ce875359ee21271937205b6dbe4。
审计并非通用符号执行器；在片段内部遇到条件分支、间接转移或返回立即拒绝。
片段入口可达性、跳表进入片段内部、被调用函数的副作用仍是显式未证明前提。

对象分类只描述调用时的地址来源：stack_relative不意味着指针不会被被调函数
保留，allocation_return不意味着对象跨决策持久，incoming不意味着全局。
allocation_return另核对PLT四条指令与AARCH64_JUMP_SLOT的_Znwm导入，
不只凭函数地址或名字猜测分配器。zero_extend32明确表示取模2^32后零扩展。
清零函数缺失也不等于泄漏；不能将局部地址分类当作快照覆盖证明。
种子中的live_in、opaque_write或call_return继续保留，不补造tick或墙钟语义。

报告必须包含全部18处，逐片段SHA和指令、确定的对象分类与未证明项。不得
因此将全部随机机制、FAIR、G0/G1或正式训练门标为通过。准入字段保持false。
