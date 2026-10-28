#define _GNU_SOURCE
#include "preference_codec.h"
#include "preference_store.h"
#include "mapped_files.h"
#include "native_backup_queue.h"

#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

static PreferenceCodecs *codecs;
static PreferenceStore *store;
static PreferenceBackend backend;
static NativeBackupQueue *backup_queue;
static int (*readable_memory)(uintptr_t, size_t);

static void fail(const char *message) {
    fprintf(stderr, "PREFERENCE_BACKEND_FATAL\t%s\n", message);
    fflush(NULL);
    if (store) preference_store_close(store);
    if (backup_queue) native_backup_queue_close(backup_queue);
    preference_codecs_close(codecs);
    _exit(96);
}

static int request_backup(void *context) {
    (void)context;
    if (backup_queue) {
        int created = 0;
        int result = native_backup_queue_request(backup_queue, &created);
        printf("PREFERENCE_NATIVE_BACKUP_QUEUE\t{\"result\":%d,\"created\":%s,\"delivery\":\"%s\",\"android_parity\":\"NOT_VERIFIED\"}\n",
               result, created ? "true" : "false", result ? "FAILED" : "PENDING");
        return result;
    }
    fprintf(stderr, "PREFERENCE_BACKUP\tUNRESOLVED\tTID\t%ld\n", syscall(SYS_gettid));
    return -1;
}

static int unresolved_exception(void *context, unsigned int slot) {
    (void)context;
    fprintf(stderr, "PREFERENCE_DECODE_EXCEPTION\tUNRESOLVED\tSLOT\t%u\n", slot);
    return -1;
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

__attribute__((noinline)) void preference_store_bridge(const void *key, const void *value) {
    char key_text[4096], value_text[4096];
    text(key, key_text);
    if (strcmp(key_text, "LOADING_SCREEN_STATE_LABEL")) fail("unreviewed configuration key");
    if (value) text(value, value_text);
    int result = preference_contract_write(&backend, key_text, value ? value_text : NULL);
    PreferenceStoreSummary summary;
    if (preference_store_summary(store, &summary)) fail("summary unavailable");
    printf("PREFERENCE_BACKEND_WRITE\t{\"result\":%d,\"deleted\":%s,\"commits\":%" PRIu64
           ",\"disk_writes\":%" PRIu64 ",\"false_commits\":%" PRIu64
           ",\"backup_requests\":%" PRIu64 ",\"backup_failures\":%" PRIu64
           ",\"decode_exceptions\":%" PRIu64 ",\"entries\":[%zu,%zu]}\n",
           result, value ? "false" : "true", summary.commits, summary.disk_writes,
           summary.false_commits, summary.backup_requests, summary.backup_failures,
           summary.decode_exceptions, summary.entries[0], summary.entries[1]);
    if (result) fail(summary.backup_failures ? "backup service unresolved after persistence" :
                     "configuration contract failed");
}

int native_config_install(uintptr_t base, int (*readable)(uintptr_t, size_t)) {
    const char *directory = getenv("NASHURA_PREFERENCE_DIRECTORY");
    const char *material = getenv("NASHURA_PREFERENCE_MATERIAL");
    const char *library = getenv("NASHURA_PREFERENCE_CRYPTO");
    const char *expected = getenv("NASHURA_CONFIG_WRITE_GUARD");
    uintptr_t target = base + 0x140b7b4;
    if (!directory || !material || !library || !expected || strlen(expected) != 32 || !readable(target, 16)) return 96;
    unsigned char bytes[16];
    for (size_t index = 0; index < sizeof(bytes); index++) {
        unsigned int value;
        if (sscanf(expected + index * 2, "%2x", &value) != 1) return 96;
        bytes[index] = value;
    }
    if (memcmp((void *)target, bytes, sizeof(bytes))) return 96;
    if (preference_codecs_open_material(library, material, &codecs)) return 96;
    PreferenceStoreOptions options = {codecs, preference_codecs_transform, unresolved_exception,
                                     request_backup, NULL, NULL};
    if (preference_store_open(directory, &options, &store) || preference_store_backend(store, &backend)) {
        if (store) preference_store_close(store);
        preference_codecs_close(codecs);
        return 96;
    }
    const char *backup_mode = getenv("NASHURA_PREFERENCE_BACKUP_BACKEND");
    if (backup_mode && (strcmp(backup_mode, "native-filesystem-queue-v1") ||
                        native_backup_queue_open(directory, &backup_queue)))
        fail("unknown or unavailable native backup backend");
#if defined(__aarch64__)
    readable_memory = readable;
    uint32_t instructions[] = {0x58000050, 0xd61f0200};
    uintptr_t replacement = (uintptr_t)preference_store_bridge;
    unsigned char patch[16];
    memcpy(patch, instructions, 8);
    memcpy(patch + 8, &replacement, 8);
    long page_size = sysconf(_SC_PAGESIZE);
    if (page_size <= 0) fail("page size");
    uintptr_t start = target & ~((uintptr_t)page_size - 1);
    size_t length = ((target + 16 + page_size - 1) & ~((uintptr_t)page_size - 1)) - start;
    if (mprotect((void *)start, length, PROT_READ | PROT_WRITE)) fail("write protection");
    memcpy((void *)target, patch, sizeof(patch));
    __builtin___clear_cache((char *)target, (char *)(target + sizeof(patch)));
    if (mprotect((void *)start, length, PROT_READ | PROT_EXEC)) fail("execute protection");
    observe_mapped_files();
    puts(backup_queue ? "PREFERENCE_BACKEND_HOOK\thistorical_ascii_native_backup_candidate_parity_unverified" :
                       "PREFERENCE_BACKEND_HOOK\thistorical_identity_ascii_xml_only_backup_unresolved");
    return 0;
#else
    (void)readable;
    fail("AArch64 required");
    return 96;
#endif
}
