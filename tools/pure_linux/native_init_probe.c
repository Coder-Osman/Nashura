#define _GNU_SOURCE
#include <dlfcn.h>
#include <inttypes.h>
#include <link.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

static uintptr_t engine_base;

static int mappings(struct dl_phdr_info *information, size_t size, void *argument) {
    (void)size;
    (void)argument;
    printf("PHDR\t%" PRIxPTR "\t%s\n", (uintptr_t)information->dlpi_addr, information->dlpi_name);
    const char *name = strrchr(information->dlpi_name, '/');
    name = name ? name + 1 : information->dlpi_name;
    if (strcmp(name, "libg.so") == 0) engine_base = information->dlpi_addr;
    return 0;
}

static int readable(uintptr_t address, size_t length) {
    address &= UINT64_C(0x00ffffffffffffff);
    if (!address || address + length < address) return 0;
    FILE *stream = fopen("/proc/self/maps", "r");
    if (!stream) return 0;
    char line[1024];
    uintptr_t start, end;
    char permissions[5];
    int result = 0;
    while (fgets(line, sizeof(line), stream)) {
        if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s", &start, &end, permissions) == 3 &&
            permissions[0] == 'r' && start <= address && address + length <= end) {
            result = 1;
            break;
        }
    }
    fclose(stream);
    return result;
}

static int save_maps(const char *name) {
    FILE *source = fopen("/proc/self/maps", "r");
    FILE *destination = fopen(name, "wx");
    if (!source || !destination) {
        if (source) fclose(source);
        if (destination) fclose(destination);
        return 1;
    }
    char block[4096];
    size_t count;
    int failed = 0;
    while ((count = fread(block, 1, sizeof(block), source)) != 0) {
        if (fwrite(block, 1, count, destination) != count) failed = 1;
    }
    failed |= ferror(source);
    failed |= fclose(destination) != 0;
    fclose(source);
    return failed;
}

static int inspect_content(void) {
    uintptr_t root = 0, context = 0;
    uintptr_t slot = engine_base + 0x019ad878;
    if (!engine_base || !readable(slot, sizeof(root))) return 21;
    memcpy(&root, (void *)slot, sizeof(root));
    printf("CONTENT_ROOT\t%" PRIxPTR "\n", root);
    if (!root) return 22;
    if (!readable(root + 0x20, sizeof(context))) return 23;
    memcpy(&context, (void *)(root + 0x20), sizeof(context));
    printf("CONTENT_CONTEXT\t%" PRIxPTR "\n", context);
    if (!context || !readable(context + 0x6e8, 1)) return 24;
    printf("CONTENT_READY\t%u\n", *(const unsigned char *)(context + 0x6e8));
    uintptr_t objects[] = {root, context};
    size_t lengths[] = {0x30, 0xb70};
    for (size_t index = 0; index < 2; ++index) {
        if (!readable(objects[index], lengths[index])) return 25;
        uint64_t checksum = UINT64_C(14695981039346656037);
        for (size_t offset = 0; offset < lengths[index]; ++offset) {
            checksum ^= *(const unsigned char *)(objects[index] + offset);
            checksum *= UINT64_C(1099511628211);
        }
        printf("OBJECT_FNV64\t%zu\t%zu\t%016" PRIx64 "\n", index, lengths[index], checksum);
    }
    return 0;
}

static int bootstrap(const char *expected_hex) {
    if (strlen(expected_hex) != 32 || !readable(engine_base + 0x8001a4, 16)) return 30;
    unsigned char expected[16];
    for (size_t index = 0; index < sizeof(expected); ++index) {
        unsigned int value;
        if (sscanf(expected_hex + index * 2, "%2x", &value) != 1) return 31;
        expected[index] = value;
    }
    if (memcmp((void *)(engine_base + 0x8001a4), expected, sizeof(expected)) != 0) return 32;
    uintptr_t root;
    memcpy(&root, (void *)(engine_base + 0x019ad878), sizeof(root));
    if (root != 0) return 33;
    puts("PHASE\tcontent_bootstrap_enter");
    ((void (*)(void))(engine_base + 0x8001a4))();
    puts("PHASE\tcontent_bootstrap_return");
    return inspect_content();
}

static int filesystem_bootstrap(const char *expected_hex) {
    uintptr_t slot = engine_base + 0x019f8d50;
    uintptr_t manager = 0;
    if (!readable(slot, sizeof(manager))) return 40;
    memcpy(&manager, (void *)slot, sizeof(manager));
    printf("FILESYSTEM_BEFORE\t%" PRIxPTR "\n", manager);
    if (manager != 0) return 41;
    if (strlen(expected_hex) != 32 || !readable(engine_base + 0x12dfed4, 16)) return 42;
    unsigned char expected[16];
    for (size_t index = 0; index < sizeof(expected); ++index) {
        unsigned int value;
        if (sscanf(expected_hex + index * 2, "%2x", &value) != 1) return 43;
        expected[index] = value;
    }
    if (memcmp((void *)(engine_base + 0x12dfed4), expected, sizeof(expected)) != 0) return 44;
    printf("PHASE\tfilesystem_bootstrap_enter\tTID\t%ld\n", syscall(SYS_gettid));
    ((void (*)(void))(engine_base + 0x12dfed4))();
    puts("PHASE\tfilesystem_bootstrap_return");
    memcpy(&manager, (void *)slot, sizeof(manager));
    printf("FILESYSTEM_AFTER\t%" PRIxPTR "\n", manager);
    if (!readable(manager, 0x188)) return 45;
    uint64_t checksum = UINT64_C(14695981039346656037);
    for (size_t offset = 0; offset < 0x188; ++offset) {
        checksum ^= *(const unsigned char *)(manager + offset);
        checksum *= UINT64_C(1099511628211);
    }
    printf("FILESYSTEM_FNV64\t392\t%016" PRIx64 "\n", checksum);
    puts("FILESYSTEM_COVERAGE\tallocation_only_no_provider_or_catalog");
    return 0;
}

__attribute__((noinline, visibility("default"))) void native_init_loaded_marker(void) {
    __asm__ volatile("" ::: "memory");
    const char *observe = getenv("NASHURA_OBSERVE_TRAP");
    if (observe && strcmp(observe, "1") == 0) raise(SIGTRAP);
}

int main(int argument_count, char **arguments) {
    if (argument_count < 2 || argument_count > 4) return 2;
    setvbuf(stdout, NULL, _IONBF, 0);
    printf("PHASE\tbefore_load\tTID\t%ld\n", syscall(SYS_gettid));
    if (save_maps("before.maps")) return 3;
    void *handle = dlopen(arguments[1], RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        fprintf(stderr, "DLOPEN_FAILED\t%s\n", dlerror());
        return 4;
    }
    puts("PHASE\tafter_load");
    if (save_maps("after.maps")) return 5;
    if (dl_iterate_phdr(mappings, NULL) != 0) return 6;
    dlerror();
    void *jni = dlsym(handle, "JNI_OnLoad");
    const char *error = dlerror();
    printf("JNI_SYMBOL_ONLY\t%p\t%s\n", jni, error ? error : "resolved_not_called");
    int content = inspect_content();
    printf("CONTENT_INSPECTION\t%d\n", content);
    native_init_loaded_marker();
    if (argument_count >= 3) {
        int result = bootstrap(arguments[2]);
        printf("BOOTSTRAP_RESULT\t%d\n", result);
        if (result != 0) return result;
    }
    if (argument_count == 4) {
        int result = filesystem_bootstrap(arguments[3]);
        printf("FILESYSTEM_RESULT\t%d\n", result);
        if (result != 0) return result;
    }
    puts("PHASE\tbefore_close");
    if (dlclose(handle) != 0) return 7;
    puts("PHASE\tafter_close");
    if (save_maps("closed.maps")) return 8;
    return 0;
}
