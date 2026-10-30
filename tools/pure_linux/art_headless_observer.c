/* Per-process logs and same-thread JNI registration observation, without patching libg. */
#define _GNU_SOURCE
#include <android/log.h>
#include <dlfcn.h>
#include <inttypes.h>
#include <jni.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <fcntl.h>
#include <unistd.h>
#include <dirent.h>
#include <signal.h>
#include <stdatomic.h>
#include <stdlib.h>
#include <sys/syscall.h>
#include <ucontext.h>

static const struct JNINativeInterface *original;
static struct JNINativeInterface observed;
static uintptr_t engine_base;
static int sample_fd = -1;
static int sample_signal;
static _Atomic int sample_done;
static uintptr_t sample_frames[24];
static size_t sample_count;

/* Bionic malloc may return top-byte-tagged ARM64 pointers. /proc/self/mem
 * takes an untagged file offset; preserve all engine pointers themselves. */
static off_t memory_offset(uintptr_t address) {
    return (off_t)(address & UINT64_C(0x00ffffffffffffff));
}

/* A bounded self-sample: no engine hooks or return-value changes. */
static void sample_handler(int number, siginfo_t *info, void *opaque) {
    (void)number;
    (void)info;
    ucontext_t *context = opaque;
    sample_count = 0;
    sample_frames[sample_count++] = context->uc_mcontext.pc;
    sample_frames[sample_count++] = context->uc_mcontext.regs[30];
    uintptr_t frame = context->uc_mcontext.regs[29];
    while (sample_count < sizeof(sample_frames) / sizeof(sample_frames[0])) {
        uintptr_t pair[2];
        if (!frame || (frame & 15) || pread(sample_fd, pair, sizeof(pair), memory_offset(frame)) != sizeof(pair)) break;
        sample_frames[sample_count++] = pair[1];
        if (pair[0] <= frame || pair[0] - frame > 16 * 1024 * 1024) break;
        frame = pair[0];
    }
    atomic_store_explicit(&sample_done, 1, memory_order_release);
}

static void sample_mainloop(void) {
    if (!sample_signal) {
        int number = SIGRTMIN + 8;
        struct sigaction previous, action = {0};
        if (number > SIGRTMAX || sigaction(number, NULL, &previous)
                || previous.sa_handler != SIG_DFL) {
            puts("HEADLESS_MAINLOOP_SAMPLE=SIGNAL_UNAVAILABLE");
            return;
        }
        sample_fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
        if (sample_fd < 0) { puts("HEADLESS_MAINLOOP_SAMPLE=MEM_UNAVAILABLE"); return; }
        action.sa_sigaction = sample_handler;
        action.sa_flags = SA_SIGINFO | SA_RESTART;
        sigemptyset(&action.sa_mask);
        if (sigaction(number, &action, NULL)) {
            close(sample_fd);
            sample_fd = -1;
            puts("HEADLESS_MAINLOOP_SAMPLE=INSTALL_FAILED");
            return;
        }
        // Keep the handler until process exit, including after delivery timeout.
        sample_signal = number;
    }
    DIR *tasks = opendir("/proc/self/task");
    if (!tasks) { puts("HEADLESS_MAINLOOP_SAMPLE=TASKS_UNAVAILABLE"); return; }
    struct dirent *entry;
    int target = 0;
    while ((entry = readdir(tasks))) {
        char path[128], name[64];
        int tid = atoi(entry->d_name);
        if (tid <= 0) continue;
        snprintf(path, sizeof(path), "/proc/self/task/%d/comm", tid);
        FILE *stream = fopen(path, "r");
        if (!stream) continue;
        if (fgets(name, sizeof(name), stream) && !strcmp(name, "Mainloop\n")) target = tid;
        fclose(stream);
        if (target) break;
    }
    closedir(tasks);
    if (!target) { puts("HEADLESS_MAINLOOP_SAMPLE=NO_THREAD"); return; }
    atomic_store_explicit(&sample_done, 0, memory_order_relaxed);
    if (syscall(SYS_tgkill, getpid(), target, sample_signal)) {
        puts("HEADLESS_MAINLOOP_SAMPLE=SEND_FAILED");
        return;
    }
    for (int attempt = 0; attempt < 500 && !atomic_load_explicit(&sample_done, memory_order_acquire); attempt++) usleep(1000);
    if (!atomic_load_explicit(&sample_done, memory_order_acquire)) {
        puts("HEADLESS_MAINLOOP_SAMPLE=DELIVERY_TIMEOUT");
        return;
    }
    puts("HEADLESS_MAINLOOP_SAMPLE=CAPTURED");
    for (size_t index = 0; index < sample_count; index++) {
        Dl_info info = {0};
        if (dladdr((void *)sample_frames[index], &info) && info.dli_fname) {
            const char *name = strrchr(info.dli_fname, '/');
            printf("HEADLESS_MAINLOOP_FRAME=%zu module=%s offset=0x%" PRIxPTR "\n",
                   index, name ? name + 1 : info.dli_fname,
                   sample_frames[index] - (uintptr_t)info.dli_fbase);
        } else printf("HEADLESS_MAINLOOP_FRAME=%zu module=UNRESOLVED\n", index);
    }
}

static int find_engine(struct dl_phdr_info *info, size_t size, void *opaque) {
    (void)size;
    (void)opaque;
    const char *name = strrchr(info->dlpi_name, '/');
    if (name && !strcmp(name + 1, "libg.so")) engine_base = info->dlpi_addr;
    return 0;
}

static void pointer_state(int fd, const char *name, uintptr_t address, uintptr_t *value) {
    *value = 0;
    if (!address) printf("HEADLESS_%s_NONNULL=UNAVAILABLE_PARENT\n", name);
    else if (pread(fd, value, sizeof(*value), memory_offset(address)) != sizeof(*value)) {
        *value = 0;
        printf("HEADLESS_%s_NONNULL=READ_FAILED\n", name);
    } else printf("HEADLESS_%s_NONNULL=%d\n", name, *value != 0);
}

static void object_details(int fd, const char *name, uintptr_t object) {
    uintptr_t vtable;
    if (!object || pread(fd, &vtable, sizeof(vtable), memory_offset(object)) != sizeof(vtable)) return;
    if (vtable >= engine_base && vtable - engine_base < 0x2000000) {
        printf("HEADLESS_%s_VTABLE_OFFSET=0x%" PRIxPTR "\n", name, vtable - engine_base);
    } else printf("HEADLESS_%s_VTABLE_OFFSET=OUTSIDE_ENGINE\n", name);
}

static void log_message(const struct __android_log_message *message) {
    fprintf(stderr, "HEADLESS_LOG priority=%d tag=%s %s\n", message->priority,
            message->tag ? message->tag : "", message->message ? message->message : "");
    fflush(stderr);
}

static jint register_natives(JNIEnv *env, jclass cls, const JNINativeMethod *methods, jint count) {
    for (jint index = 0; index < count; index++) {
        Dl_info info = {0};
        if (dladdr(methods[index].fnPtr, &info) && info.dli_fname && strstr(info.dli_fname, "libg.so")) {
            printf("HEADLESS_NATIVE_BIND name=%s signature=%s offset=0x%" PRIxPTR "\n",
                   methods[index].name, methods[index].signature,
                   (uintptr_t)methods[index].fnPtr - (uintptr_t)info.dli_fbase);
        }
    }
    fflush(stdout);
    return original->RegisterNatives(env, cls, methods, count);
}

JNIEXPORT void JNICALL Java_ArtHeadlessCreateProbe_prepareObservations(JNIEnv *env, jclass cls) {
    (void)cls;
    __android_log_set_logger(log_message);
    original = *env;
    memcpy(&observed, original, sizeof(observed));
    observed.RegisterNatives = register_natives;
    *env = &observed;
}

JNIEXPORT void JNICALL Java_ArtHeadlessCreateProbe_finishObservations(JNIEnv *env, jclass cls) {
    (void)cls;
    if (*env == &observed) *env = original;
}

JNIEXPORT void JNICALL Java_ArtHeadlessCreateProbe_inspectState(JNIEnv *env, jclass cls) {
    (void)env;
    (void)cls;
    engine_base = 0;
    dl_iterate_phdr(find_engine, NULL);
    int fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
    if (fd < 0 || !engine_base) {
        if (fd >= 0) close(fd);
        puts("HEADLESS_STATE_READ_UNAVAILABLE");
        fflush(stdout);
        return;
    }
    uintptr_t owner, holder, game, filesystem, root, context;
    pointer_state(fd, "OWNER", engine_base + 0x1a06ab8, &owner);
    pointer_state(fd, "HOLDER", owner, &holder);
    pointer_state(fd, "GAME", owner ? owner + 8 : 0, &game);
    uintptr_t owner_context, game_context, holder_20;
    pointer_state(fd, "OWNER_CONTEXT", owner ? owner + 0x10 : 0, &owner_context);
    pointer_state(fd, "GAME_CONTEXT", game ? game + 8 : 0, &game_context);
    pointer_state(fd, "HOLDER_RAW_20", holder ? holder + 0x20 : 0, &holder_20);
    if (owner_context && game_context) {
        printf("HEADLESS_OWNER_GAME_CONTEXT_EQUAL=%d\n", owner_context == game_context);
    } else puts("HEADLESS_OWNER_GAME_CONTEXT_EQUAL=UNAVAILABLE");
    if (owner_context && holder_20) {
        printf("HEADLESS_OWNER_HOLDER_20_EQUAL=%d\n", owner_context == holder_20);
    } else puts("HEADLESS_OWNER_HOLDER_20_EQUAL=UNAVAILABLE");
    pointer_state(fd, "FILESYSTEM", engine_base + 0x19f8d50, &filesystem);
    pointer_state(fd, "CONTENT_ROOT", engine_base + 0x19ad878, &root);
    pointer_state(fd, "CONTENT_CONTEXT", root ? root + 0x20 : 0, &context);
    object_details(fd, "HOLDER", holder);
    object_details(fd, "GAME", game);
    const unsigned int offsets[] = {0x50, 0x7b, 0x7d};
    for (size_t index = 0; owner && index < sizeof(offsets) / sizeof(offsets[0]); index++) {
        unsigned char value;
        if (pread(fd, &value, 1, memory_offset(owner + offsets[index])) == 1) {
            printf("HEADLESS_OWNER_RAW_%02x=%u\n", offsets[index], value);
        }
    }
    uint32_t game_c8;
    if (game && pread(fd, &game_c8, sizeof(game_c8), memory_offset(game + 0xc8)) == sizeof(game_c8)) {
        printf("HEADLESS_GAME_RAW_C8=%" PRIu32 "\n", game_c8);
    }
    unsigned char ready = 0;
    int ready_read = context && pread(fd, &ready, 1, memory_offset(context + 0x6e8)) == 1;
    if (ready_read) printf("HEADLESS_CONTENT_READY_RAW=%u\n", ready);
    else puts("HEADLESS_CONTENT_READY_RAW=UNAVAILABLE");
    close(fd);
    sample_mainloop();
    fflush(stdout);
}
