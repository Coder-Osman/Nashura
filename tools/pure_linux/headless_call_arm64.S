// Minimal AArch64 ABI bridges used by the headless GameState probe.
//
// These entry points target the exact libg build checked by the Java probe.
// libg returns these objects through x8 rather than the normal C return
// register; each wrapper installs the caller-provided result slot in x8 and
// forwards the remaining arguments unchanged.

    .text
    .align 2

    .global cr_call_logic_command_parse
    .type cr_call_logic_command_parse, %function
// void cr_call_logic_command_parse(void* json, void* context,
//                                  void** result, void* target)
cr_call_logic_command_parse:
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    mov x8, x2
    blr x3
    ldp x29, x30, [sp], #16
    ret
    .size cr_call_logic_command_parse, .-cr_call_logic_command_parse

    .align 2
    .global cr_call_state_snapshot
    .type cr_call_state_snapshot, %function
// void cr_call_state_snapshot(void* manager, void** result, void* target)
cr_call_state_snapshot:
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    mov x8, x1
    blr x2
    ldp x29, x30, [sp], #16
    ret
    .size cr_call_state_snapshot, .-cr_call_state_snapshot

    .align 2
    .global cr_call_card_selection_build
    .type cr_call_card_selection_build, %function
// void cr_call_card_selection_build(void* deck_slot, void* player,
//                                   int alternate, void* result, void* target)
cr_call_card_selection_build:
    stp x29, x30, [sp, #-16]!
    mov x29, sp
    mov x8, x3
    blr x4
    ldp x29, x30, [sp], #16
    ret
    .size cr_call_card_selection_build, .-cr_call_card_selection_build
