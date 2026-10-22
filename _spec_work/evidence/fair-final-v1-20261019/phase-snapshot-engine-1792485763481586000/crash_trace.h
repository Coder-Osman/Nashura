// Bounded diagnostic: record candidate original-code return addresses then exit.
// The handler never resumes a failed call or supplies a fabricated return value.
#include <ucontext.h>
static uintptr_t diagnostic_libg_base;
static void trace_hex(uintptr_t value) {
    char out[19] = "0x0000000000000000\n";
    static const char hex[] = "0123456789abcdef";
    for (int i = 17; i >= 2; --i) { out[i] = hex[value & 15]; value >>= 4; }
    (void)write(STDERR_FILENO, out, 19);
}
static void trace_fault(int signum, siginfo_t *info, void *opaque) {
    ucontext_t *ctx = opaque;
    static const char title[] = "DIAGNOSTIC_FAULT base, pc, lr, fault address, x0-x3, x8-x9, x19-x23; candidate libg stack offsets:\n";
    (void)write(STDERR_FILENO, title, sizeof(title)-1);
    trace_hex(diagnostic_libg_base);
    trace_hex(ctx->uc_mcontext.pc);
    trace_hex(ctx->uc_mcontext.regs[30]);
    trace_hex((uintptr_t)info->si_addr);
    for (int i = 0; i < 4; ++i) trace_hex(ctx->uc_mcontext.regs[i]);
    trace_hex(ctx->uc_mcontext.regs[8]);
    trace_hex(ctx->uc_mcontext.regs[9]);
    for (int i = 19; i <= 23; ++i) trace_hex(ctx->uc_mcontext.regs[i]);
    const uintptr_t *stack = (const uintptr_t *)ctx->uc_mcontext.sp;
    for (int i = 0; i < 128; ++i) {
        uintptr_t addr = stack[i];
        if (addr >= diagnostic_libg_base && addr < diagnostic_libg_base + 0x01a80000)
            trace_hex(addr - diagnostic_libg_base);
    }
    _exit(128 + signum);
}
static void install_fault_diagnostic(uintptr_t base) {
    diagnostic_libg_base = base;
    struct sigaction sa = {0};
    sa.sa_sigaction = trace_fault;
    sa.sa_flags = SA_SIGINFO;
    sigemptyset(&sa.sa_mask);
    sigaction(SIGSEGV, &sa, NULL);
    sigaction(SIGABRT, &sa, NULL);
}
