/* Freestanding Linux AArch64 syscall test; no Android, libc, or game code. */
#ifndef EXIT_CODE
#define EXIT_CODE 0
#endif
typedef unsigned long U64;
static long sys6(long n, long a, long b, long c, long d, long e, long f) {
    register long x8 __asm__("x8") = n;
    register long x0 __asm__("x0") = a;
    register long x1 __asm__("x1") = b;
    register long x2 __asm__("x2") = c;
    register long x3 __asm__("x3") = d;
    register long x4 __asm__("x4") = e;
    register long x5 __asm__("x5") = f;
    __asm__ volatile("svc #0" : "+r"(x0) : "r"(x8), "r"(x1), "r"(x2),
                     "r"(x3), "r"(x4), "r"(x5) : "memory", "cc");
    return x0;
}
#define S(n,a,b,c,d) sys6(n,(long)(a),(long)(b),(long)(c),(long)(d),0,0)
static void log_line(const char *s) {
    long n = 0; while (s[n]) ++n;
    if (S(64, 1, s, n, 0) != n) S(94, 90, 0, 0, 0);
}
static int pipefd[2];
static volatile long thread_tid, thread_pid;
static unsigned char child_stack[65536] __attribute__((aligned(16)));
extern long spawn_thread(void *stack);
void smoke_thread(void) {
    thread_tid = S(178, 0, 0, 0, 0);
    thread_pid = S(172, 0, 0, 0, 0);
    char token = 'T';
    S(64, pipefd[1], &token, 1, 0);
    S(93, 0, 0, 0, 0);
    __builtin_unreachable();
}
int smoke_main(void) {
    long pid = S(172, 0, 0, 0, 0), tid = S(178, 0, 0, 0, 0);
    if (pid <= 0 || tid != pid) return 11;
    log_line("{\"check\":\"process_syscall\",\"pass\":true}\n");
    long t0[2], t1[2];
    if (S(113, 1, t0, 0, 0) || S(113, 1, t1, 0, 0)) return 12;
    if (t0[1] < 0 || t0[1] >= 1000000000 || t1[0] < t0[0] ||
        (t1[0] == t0[0] && t1[1] < t0[1])) return 13;
    log_line("{\"check\":\"monotonic_clock\",\"pass\":true}\n");
    long fd = S(56, -100, "smoke-file.txt", 0xc2, 0600);
    char want[] = "Nashura file canary", got[sizeof(want)];
    if (fd < 0 || S(64, fd, want, sizeof(want), 0) != sizeof(want) ||
        S(62, fd, 0, 0, 0) != 0 || S(63, fd, got, sizeof(got), 0) != sizeof(got)) return 14;
    for (unsigned i = 0; i < sizeof(want); ++i) if (want[i] != got[i]) return 15;
    if (S(57, fd, 0, 0, 0)) return 16;
    log_line("{\"check\":\"file_io\",\"pass\":true}\n");
    int pair[2]; char value = 'U', received = 0;
    if (S(199, 1, 1, 0, pair) || S(64, pair[0], &value, 1, 0) != 1 ||
        S(63, pair[1], &received, 1, 0) != 1 || received != value) return 17;
    S(57, pair[0], 0, 0, 0); S(57, pair[1], 0, 0, 0);
    log_line("{\"check\":\"unix_socket_ipc\",\"pass\":true}\n");
    if (S(59, pipefd, 0, 0, 0)) return 18;
    long child = spawn_thread(child_stack + sizeof(child_stack));
    if (child <= 0 || S(63, pipefd[0], &received, 1, 0) != 1 || received != 'T' ||
        thread_pid != pid || thread_tid == tid || thread_tid != child) return 19;
    S(57, pipefd[0], 0, 0, 0); S(57, pipefd[1], 0, 0, 0);
    log_line("{\"check\":\"clone_thread_shared_state\",\"pass\":true}\n");
    U64 mask = 1UL << 9; /* SIGUSR1=10; kernel signal set is 8 bytes. */
    unsigned int si[32];
    if (S(135, 0, &mask, 0, 8)) return 20;
    long sfd = S(74, -1, &mask, 8, 0);
    if (sfd < 0 || S(131, pid, tid, 10, 0) ||
        S(63, sfd, si, sizeof(si), 0) != sizeof(si) || si[0] != 10 || si[3] != pid) return 21;
    S(57, sfd, 0, 0, 0);
    log_line("{\"check\":\"signal_delivery\",\"pass\":true}\n");
    log_line("{\"check\":\"all_checks\",\"pass\":true}\n");
    return EXIT_CODE;
}
