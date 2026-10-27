#define _GNU_SOURCE
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

extern int loading_config_init(const char *, int);
extern int loading_config_write(const char *, const char *, uint64_t *);
static int (*readable_memory)(uintptr_t, size_t);

static void fail(const char *message) {
    fprintf(stderr, "LOADING_CONFIG_FATAL\t%s\n", message);
    _exit(96);
}

static void text(const void *object, char *output) {
    uintptr_t address = (uintptr_t)object & UINT64_C(0x00ffffffffffffff);
    if (!readable_memory(address, 16)) fail("unreadable string object");
    int32_t length;
    memcpy(&length, (void *)(address + 4), 4);
    if (length < 0 || length > 4095) fail("string length");
    uintptr_t pointer = address + 8;
    if (length >= 8) memcpy(&pointer, (void *)(address + 8), 8);
    pointer &= UINT64_C(0x00ffffffffffffff);
    if (!readable_memory(pointer, (size_t)length + 1) || *(char *)(pointer + length)) fail("string payload");
    memcpy(output, (void *)pointer, (size_t)length + 1);
    if (strlen(output) != (size_t)length) fail("embedded terminator");
}

__attribute__((noinline)) void loading_config_store_bridge(const void *key, const void *value) {
    char key_text[4096], value_text[4096];
    text(key, key_text);
    if (value) text(value, value_text);
    uint64_t backups;
    if (loading_config_write(key_text, value ? value_text : NULL, &backups)) fail("contract or persistence");
    printf("LOADING_CONFIG_WRITE\t%s\t%s\t%" PRIu64 "\tTID\t%ld\n",
           key_text, value ? value_text : "<deleted>", backups, syscall(SYS_gettid));
}

int native_config_install(uintptr_t base, int (*readable)(uintptr_t, size_t)) {
    const char *path = getenv("NASHURA_LOADING_CONFIG_DIRECTORY");
    if (!path) return 0;
    const char *expected = getenv("NASHURA_CONFIG_WRITE_GUARD");
    uintptr_t target = base + 0x140b7b4;
    if (!expected || strlen(expected) != 32 || !readable(target, 16)) return 96;
    unsigned char bytes[16];
    for (size_t index = 0; index < sizeof(bytes); ++index) {
        unsigned int value;
        if (sscanf(expected + index * 2, "%2x", &value) != 1) return 96;
        bytes[index] = value;
    }
    if (memcmp((void *)target, bytes, sizeof(bytes))) return 96;
    if (loading_config_init(path, 1)) return 96;
#if defined(__aarch64__)
    readable_memory = readable;
    uint32_t instructions[] = {0x58000050, 0xd61f0200};
    uintptr_t replacement = (uintptr_t)loading_config_store_bridge;
    unsigned char patch[16];
    memcpy(patch, instructions, 8);
    memcpy(patch + 8, &replacement, 8);
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) return 96;
    uintptr_t start = target & ~((uintptr_t)page_size - 1);
    size_t length = ((target + 16 + page_size - 1) & ~((uintptr_t)page_size - 1)) - start;
    if (mprotect((void *)start, length, PROT_READ | PROT_WRITE)) return 96;
    memcpy((void *)target, patch, sizeof(patch));
    __builtin___clear_cache((char *)target, (char *)(target + sizeof(patch)));
    if (mprotect((void *)start, length, PROT_READ | PROT_EXEC)) return 96;
    puts("LOADING_CONFIG_HOOK\tisolated_native_candidate_no_android_backup_parity");
    return 0;
#else
    (void)readable;
    return 96;
#endif
}
