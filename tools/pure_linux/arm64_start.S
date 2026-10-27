.text
.global _start
_start:
    ldr x0, [sp]
    add x1, sp, #8
    bl smoke_main
    mov x8, #94
    svc #0

.global spawn_thread
spawn_thread:
    mov x5, x0
    mov x6, x1
    mov x1, x2
    mov x4, #0
    mov x2, #0
    mov x0, #0x0f00
    movk x0, #0xd, lsl #16
    mov x8, #220
    svc #0
    cbnz x0, 1f
    mov x0, x6
    blr x5
    mov x0, #0
    mov x8, #93
    svc #0
1:
    ret
.section .note.GNU-stack,"",%progbits
