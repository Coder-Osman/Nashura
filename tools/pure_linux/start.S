.text
.global _start
.type _start,%function
_start:
    bl smoke_main
    mov x8,#94
    svc #0
    brk #0
.global spawn_thread
.type spawn_thread,%function
spawn_thread:
    mov x1,x0
    mov x0,#0x0f00
    movk x0,#5,lsl #16
    mov x2,#0
    mov x3,#0
    mov x4,#0
    mov x8,#220
    svc #0
    cbz x0,1f
    ret
1:  bl smoke_thread
    brk #0
.section .note.GNU-stack,"",%progbits
