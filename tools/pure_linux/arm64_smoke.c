typedef unsigned long word;
struct timespec_value { long seconds; long nanoseconds; };
struct tls_canary { word value; };
static unsigned char thread_stack[65536] __attribute__((aligned(16)));
static struct tls_canary parent_tls = { 0x12345678 }, child_tls = { 0xabcdef01 };
static int channel[2];
static volatile word child_result;
extern long spawn_thread(void (*entry)(void *), void *argument, void *stack, void *tls);

static long syscall6(long number, long first, long second, long third, long fourth, long fifth, long sixth) {
    register long reg0 __asm__("x0") = first;
    register long reg1 __asm__("x1") = second;
    register long reg2 __asm__("x2") = third;
    register long reg3 __asm__("x3") = fourth;
    register long reg4 __asm__("x4") = fifth;
    register long reg5 __asm__("x5") = sixth;
    register long reg8 __asm__("x8") = number;
    __asm__ volatile("svc #0" : "+r"(reg0) : "r"(reg1), "r"(reg2), "r"(reg3), "r"(reg4), "r"(reg5), "r"(reg8) : "memory", "cc");
    return reg0;
}

static long call3(long number, long first, long second, long third) {
    return syscall6(number, first, second, third, 0, 0, 0);
}

static word length(const char *text) {
    word count = 0;
    while (text[count]) count++;
    return count;
}

static int equal(const char *left, const char *right, word size) {
    for (word index = 0; index < size; index++) if (left[index] != right[index]) return 0;
    return 1;
}

static int check(const char *name, int passed) {
    const char *prefix = "{\"check\":\"";
    const char *suffix = passed ? "\",\"pass\":true}\n" : "\",\"pass\":false}\n";
    if (call3(64, 1, (long)prefix, length(prefix)) < 0) return 0;
    if (call3(64, 1, (long)name, length(name)) < 0) return 0;
    if (call3(64, 1, (long)suffix, length(suffix)) < 0) return 0;
    return passed;
}

static void thread_entry(void *argument) {
    struct tls_canary *current;
    __asm__ volatile("mrs %0, tpidr_el0" : "=r"(current));
    child_result = current == &child_tls && current->value == 0xabcdef01 && argument == &child_tls;
    current->value = 0xfeedface;
    char marker = 'T';
    call3(64, channel[1], (long)&marker, 1);
}

long smoke_main(long argument_count, char **arguments) {
    if (argument_count == 2 && equal(arguments[1], "--exit-23", 10)) {
        return check("exit_code_probe", 1) ? 23 : 1;
    }
    int passed = check("process_syscall", call3(172, 0, 0, 0) > 0 && call3(178, 0, 0, 0) > 0);
    struct timespec_value before, after;
    int clock_ok = call3(113, 1, (long)&before, 0) == 0 && call3(113, 1, (long)&after, 0) == 0;
    passed &= check("monotonic_clock", clock_ok && (after.seconds > before.seconds ||
                     (after.seconds == before.seconds && after.nanoseconds >= before.nanoseconds)));
    const char contents[] = "Nashura direct ARM64 Linux canary\n";
    char buffer[sizeof(contents)] = {0};
    long file = syscall6(56, -100, (long)"smoke-file.txt", 0xc2, 0600, 0, 0);
    int file_ok = file >= 0 && call3(64, file, (long)contents, sizeof(contents)) == sizeof(contents);
    file_ok &= call3(62, file, 0, 0) == 0;
    file_ok &= call3(63, file, (long)buffer, sizeof(buffer)) == sizeof(buffer);
    file_ok &= equal(buffer, contents, sizeof(contents));
    file_ok &= call3(57, file, 0, 0) == 0;
    passed &= check("file_io", file_ok);
    int sockets[2];
    int ipc_ok = syscall6(199, 1, 1, 0, (long)sockets, 0, 0) == 0;
    if (ipc_ok) {
        ipc_ok &= call3(64, sockets[0], (long)contents, sizeof(contents)) == sizeof(contents);
        ipc_ok &= call3(63, sockets[1], (long)buffer, sizeof(buffer)) == sizeof(buffer);
        ipc_ok &= equal(buffer, contents, sizeof(contents));
        ipc_ok &= call3(57, sockets[0], 0, 0) == 0 && call3(57, sockets[1], 0, 0) == 0;
    }
    passed &= check("unix_socket_ipc", ipc_ok);
    __asm__ volatile("msr tpidr_el0, %0" :: "r"(&parent_tls) : "memory");
    long created = -1;
    int pipe_ok = call3(59, (long)channel, 0, 0) == 0;
    if (pipe_ok) created = spawn_thread(thread_entry, &child_tls, thread_stack + sizeof(thread_stack), &child_tls);
    char marker = 0;
    int thread_ok = pipe_ok && created > 0 && call3(63, channel[0], (long)&marker, 1) == 1 && marker == 'T';
    struct tls_canary *current;
    __asm__ volatile("mrs %0, tpidr_el0" : "=r"(current));
    passed &= check("clone_thread_shared_state", thread_ok && child_result == 1);
    passed &= check("tls_isolation", thread_ok && current == &parent_tls && parent_tls.value == 0x12345678 && child_tls.value == 0xfeedface);
    if (pipe_ok) {
        call3(57, channel[0], 0, 0);
        call3(57, channel[1], 0, 0);
    }
    word mask = 1UL << 9;
    unsigned char signal_info[128] __attribute__((aligned(8))) = {0};
    int signal_ok = syscall6(135, 0, (long)&mask, 0, 8, 0, 0) == 0;
    long signal_fd = syscall6(74, -1, (long)&mask, 8, 0, 0, 0);
    signal_ok &= signal_fd >= 0 && call3(131, call3(172, 0, 0, 0), call3(178, 0, 0, 0), 10) == 0;
    signal_ok &= call3(63, signal_fd, (long)signal_info, sizeof(signal_info)) == sizeof(signal_info);
    signal_ok &= *(unsigned int *)signal_info == 10;
    if (signal_fd >= 0) call3(57, signal_fd, 0, 0);
    passed &= check("signal_delivery", signal_ok);
    passed &= check("all_checks", passed);
    return passed ? 0 : 1;
}
