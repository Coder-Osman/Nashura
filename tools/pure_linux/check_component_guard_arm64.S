// Capture return and all integer callee-saved registers across the real body.
.text
.align 2
.global component_guard_invoke
.type component_guard_invoke,%function
component_guard_invoke:
    stp x29,x30,[sp,#-128]!
    stp x19,x20,[sp,#32]
    stp x21,x22,[sp,#48]
    stp x23,x24,[sp,#64]
    stp x25,x26,[sp,#80]
    stp x27,x28,[sp,#96]
    str x6,[sp,#16]
    mov x16,x0
    mov x0,x1
    mov w1,w2
    mov w2,w3
    mov w3,w4
    mov x4,x5
    mov x19,#0x119
    mov x20,#0x11a
    mov x21,#0x11b
    mov x22,#0x11c
    mov x23,#0x11d
    mov x24,#0x11e
    mov x25,#0x11f
    mov x26,#0x120
    mov x27,#0x121
    mov x28,#0x122
    mov x29,#0x123
    blr x16
    ldr x6,[sp,#16]
    str x0,[x6]
    stp x19,x20,[x6,#8]
    stp x21,x22,[x6,#24]
    stp x23,x24,[x6,#40]
    stp x25,x26,[x6,#56]
    stp x27,x28,[x6,#72]
    str x29,[x6,#88]
    ldp x19,x20,[sp,#32]
    ldp x21,x22,[sp,#48]
    ldp x23,x24,[sp,#64]
    ldp x25,x26,[sp,#80]
    ldp x27,x28,[sp,#96]
    ldp x29,x30,[sp],#128
    ret
.size component_guard_invoke,.-component_guard_invoke
