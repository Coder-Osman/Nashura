// Execute the unchanged 76-byte blocks with their original branch displacements.
// Landing stubs tag each external exit; capture only before restoring the ABI.
.text
.align 2
.global query_dimension_invoke
.type query_dimension_invoke, %function
// void invoke(code, grid, x, y, filter, output)
query_dimension_invoke:
    stp x29, x30, [sp, #-112]!
    stp x19, x22, [sp, #32]
    stp x23, x24, [sp, #48]
    stp x25, x27, [sp, #64]
    mov x16, x0
    mov x23, x1
    mov w24, w2
    mov w27, w3
    str w4, [sp, #16]
    str x5, [sp, #24]
    mov x8, #0x555
    mov x9, #0x555
    mov x25, #0x555
    mov x22, #0x555
    mov x19, #0x555
    blr x16
    ldr x5, [sp, #24]
    stp x8, x9, [x5]
    stp x25, x22, [x5, #16]
    str x19, [x5, #32]
    mrs x10, nzcv
    str x10, [x5, #40]
    str x0, [x5, #48]
    ldp x19, x22, [sp, #32]
    ldp x23, x24, [sp, #48]
    ldp x25, x27, [sp, #64]
    ldp x29, x30, [sp], #112
    ret
.size query_dimension_invoke, .-query_dimension_invoke
