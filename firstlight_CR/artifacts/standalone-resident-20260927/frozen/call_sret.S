// Original C++ nontrivial return ABI: x8 holds the caller-owned result storage.
// Arguments: function, result, first argument, second argument. No interception.
.text
.global call_sret2
.type call_sret2, %function
call_sret2:
    mov x9, x0
    mov x8, x1
    mov x0, x2
    mov x1, x3
    br x9
.size call_sret2, .-call_sret2
