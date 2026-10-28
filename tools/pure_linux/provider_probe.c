#define _GNU_SOURCE
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mman.h>
#include <sys/syscall.h>
#include <unistd.h>

typedef struct {
    uint64_t before[2];
    unsigned char object[24];
    uint64_t after[2];
} GuardedString;

static uintptr_t base;
static int (*is_readable)(uintptr_t, size_t);
static const uint64_t canary = UINT64_C(0x31594850524f5644);

static uintptr_t pointer_at(uintptr_t location) {
    uintptr_t result = 0;
    if (is_readable(location, sizeof(result))) memcpy(&result, (void *)location, sizeof(result));
    return result;
}

static int guard(const char *name, uintptr_t offset) {
    const char *expected = getenv(name);
    if (!expected || strlen(expected) != 32 || !is_readable(base + offset, 16)) return 0;
    unsigned char bytes[16];
    for (size_t index = 0; index < sizeof(bytes); ++index) {
        unsigned int value;
        if (sscanf(expected + index * 2, "%2x", &value) != 1) return 0;
        bytes[index] = value;
    }
    return memcmp((void *)(base + offset), bytes, sizeof(bytes)) == 0;
}

static int exercise_allocator(void) {
    if (!guard("NASHURA_ALLOCATOR_CTOR_GUARD", 0x1237348) ||
        !guard("NASHURA_ALLOCATOR_ALLOC_GUARD", 0x1237358) ||
        !guard("NASHURA_ALLOCATOR_FREE_GUARD", 0x12373a4)) return 0;
    const char *name = (const char *)(base + 0x2ba51e);
    if (!is_readable((uintptr_t)name, sizeof("AndroidMainAllocator")) ||
        memcmp(name, "AndroidMainAllocator", sizeof("AndroidMainAllocator"))) return 0;
    struct { uint64_t before; uintptr_t object[2]; uint64_t after; } holder = {canary, {0, 0}, canary};
    ((void (*)(void *, const char *))(base + 0x1237348))(holder.object, name);
    uintptr_t table = holder.object[0] & UINT64_C(0x00ffffffffffffff);
    if (holder.before != canary || holder.after != canary || table != base + 0x18bf798 ||
        holder.object[1] != (uintptr_t)name || !is_readable(table, 16) ||
        pointer_at(table) != base + 0x1237358 || pointer_at(table + 8) != base + 0x12373a4) return 0;
    puts("ALLOCATOR_CONSTRUCTOR\t1");
    void *block = ((void *(*)(void *, size_t, size_t))pointer_at(table))(holder.object, 64, 8);
    uintptr_t address = (uintptr_t)block & UINT64_C(0x00ffffffffffffff);
    if (!block || address % 8 || !is_readable(address, 64)) return 0;
    memset(block, 0x5a, 64);
    for (size_t i = 0; i < 64; ++i) if (((unsigned char *)block)[i] != 0x5a) return 0;
    puts("ALLOCATOR_ALLOCATION\t1");
    ((void (*)(void *, void *))pointer_at(table + 8))(holder.object, block);
    if (holder.before != canary || holder.after != canary) return 0;
    puts("ALLOCATOR_RELEASE\t1");
    if (((void *(*)(void *, size_t, size_t))pointer_at(table))(holder.object, 64, 12) != NULL ||
        ((void *(*)(void *, size_t, size_t))pointer_at(table))(holder.object, UINT64_C(0x100000000), 8) != NULL)
        return 0;
    puts("ALLOCATOR_INVALID_REJECTED\t1");
    return 1;
}

#if defined(__aarch64__)
__attribute__((noinline, used)) void app_version_return(void *output) {
    const char *version = getenv("NASHURA_APP_VERSION");
    if (!output || !version || strcmp(version, "15.535.13")) _exit(98);
    ((void (*)(void *, const char *))(base + 0x138a810))(output, version);
    uintptr_t object = (uintptr_t)output & UINT64_C(0x00ffffffffffffff);
    if (!is_readable(object, 16)) _exit(98);
    int32_t length;
    memcpy(&length, (void *)(object + 4), sizeof(length));
    if (length != (int32_t)strlen(version)) _exit(98);
    uintptr_t contents = length < 8 ? object + 8 : pointer_at(object + 8);
    contents &= UINT64_C(0x00ffffffffffffff);
    if (!is_readable(contents, (size_t)length + 1) || memcmp((void *)contents, version, (size_t)length + 1)) _exit(98);
    puts("APP_VERSION_BRIDGE_RETURN\tverified_package_version_candidate");
}

__attribute__((naked, noinline)) static void app_version_entry(void) {
    __asm__ volatile("mov x0, x8\n\tb app_version_return");
}

static int install_app_version(void) {
    if (!guard("NASHURA_APP_VERSION_WRAPPER_GUARD", 0x140cc8c) ||
        !guard("NASHURA_APP_VERSION_STRING_GUARD", 0x138a810)) return 0;
    uintptr_t target = base + 0x140cc8c, replacement = (uintptr_t)app_version_entry;
    uint32_t instructions[] = {0x58000050, 0xd61f0200};
    unsigned char patch[16];
    memcpy(patch, instructions, sizeof(instructions));
    memcpy(patch + sizeof(instructions), &replacement, sizeof(replacement));
    long page = sysconf(_SC_PAGESIZE);
    if (page <= 0) return 0;
    uintptr_t start = target & ~((uintptr_t)page - 1);
    size_t length = ((target + sizeof(patch) + page - 1) & ~((uintptr_t)page - 1)) - start;
    if (mprotect((void *)start, length, PROT_READ | PROT_WRITE)) return 0;
    memcpy((void *)target, patch, sizeof(patch));
    __builtin___clear_cache((char *)target, (char *)(target + sizeof(patch)));
    if (mprotect((void *)start, length, PROT_READ | PROT_EXEC)) return 0;
    puts("APP_VERSION_BRIDGE_INSTALLED\tverified_package_version_candidate");
    return 1;
}
#else
static int install_app_version(void) { return 0; }
#endif

static int string_valid(const GuardedString *value) {
    return value->before[0] == canary && value->before[1] == canary &&
           value->after[0] == canary && value->after[1] == canary;
}

static int string_create(GuardedString *value, const char *text) {
    memset(value, 0xa5, sizeof(*value));
    value->before[0] = value->before[1] = canary;
    value->after[0] = value->after[1] = canary;
    ((void (*)(void *, const char *, size_t))(base + 0x767f3c))(value->object, text, strlen(text));
    printf("STRING_GUARD\t%d\n", string_valid(value));
    return string_valid(value);
}

static void string_release(GuardedString *value) {
    if (value->object[0] & 1) {
        uintptr_t allocation;
        memcpy(&allocation, value->object + 16, sizeof(allocation));
        ((void (*)(void *))(base + 0x17e4b50))((void *)allocation);
    }
}

static int describe(uintptr_t manager, size_t slot, const char *name) {
    uintptr_t provider = pointer_at(manager + slot);
    uintptr_t control = pointer_at(manager + slot + 8);
    printf("PROVIDER_SLOT\t%s\t%" PRIxPTR "\t%" PRIxPTR "\n", name, provider, control);
    if (!provider || !control || !is_readable(provider, 0x60)) return 0;
    uintptr_t table = pointer_at(provider);
    printf("PROVIDER_VTABLE\t%s\t%" PRIxPTR "\n", name, table - base);
    if (table != base + (!strcmp(name, "data") ? 0x18c3370 : 0x18c3400)) return 0;
    for (size_t offset = 8; offset <= 0x20; offset += 0x18) {
        const unsigned char *value = (const unsigned char *)(provider + offset);
        size_t length = value[0] >> 1;
        uintptr_t data = (uintptr_t)(value + 1);
        if (value[0] & 1) {
            memcpy(&length, value + 8, sizeof(length));
            memcpy(&data, value + 16, sizeof(data));
        }
        if (length > 4096 || !is_readable(data, length + 1)) return 0;
        printf("PROVIDER_STRING\t%s\t%zu\t%zu\t%.*s\n", name, offset, length, (int)length, (char *)data);
        if (offset == 0x20 && (length != strlen(name) + 1 ||
            memcmp((void *)data, name, strlen(name)) || ((char *)data)[length - 1] != ':')) return 0;
    }
    return 1;
}

static void resource_exercise(void *file, const uintptr_t *entries, int valid, int64_t length) {
    if (!getenv("NASHURA_RESOURCE_EXERCISE")) return;
    int64_t details[24];
    for (size_t index = 0; index < 24; ++index) details[index] = -1;
    details[0] = file != NULL;
    int status = -1;
    if (file) details[1] = valid;
    if (file && valid) {
        details[2] = length;
        if (length >= 0 && length < INT64_MAX) {
            int64_t offsets[] = {0, length, length + 1, -1, length ? length - 1 : 0};
            unsigned char byte;
            for (size_t index = 0; index < 5; ++index) {
                size_t begin = 3 + index * 4;
                details[begin] = ((int64_t (*)(void *, int64_t))(base + entries[2]))(file, offsets[index]);
                details[begin + 1] = ((int64_t (*)(void *))(base + entries[3]))(file);
                details[begin + 2] = ((int (*)(void *))(base + entries[6]))(file);
                details[begin + 3] = ((int64_t (*)(void *, void *, int))(base + entries[1]))(file, &byte, 1);
            }
            status = 0;
        } else status = -2;
    }
    printf("RESOURCE_EXERCISE\t%d", status);
    for (size_t index = 0; index < 24; ++index) printf("\t%" PRId64, details[index]);
    putchar('\n');
}

static int read_resource(uintptr_t manager) {
    const char *name = getenv("NASHURA_RESOURCE_NAME");
    if (!name) return 0;
    if (!guard("NASHURA_OPEN_GUARD", 0x12e03e4)) return 57;
    GuardedString path;
    if (!string_create(&path, name)) return 58;
    printf("PHASE\tresource_open_enter\t%s\tTID\t%ld\n", name, syscall(SYS_gettid));
    void *file = ((void *(*)(void *, const void *, int))(base + 0x12e03e4))(
        (void *)manager, path.object, 0);
    puts("PHASE\tresource_open_return");
    if (!string_valid(&path)) return 59;
    string_release(&path);
    if (!file) {
        resource_exercise(NULL, NULL, 0, 0);
        puts("RESOURCE_MISSING\t1");
        return 60;
    }
    uintptr_t table = pointer_at((uintptr_t)file);
    const size_t indices[] = {1, 2, 4, 5, 6, 7, 12};
    const uintptr_t disk_entries[] = {0x12e3350, 0x12e3470, 0x12e3458, 0x12e3374,
                                      0x12e337c, 0x12e3384, 0x12e3438};
    const uintptr_t apk_entries[] = {0x12e7990, 0x12e7aa8, 0x12e7a9c, 0x12e7a54,
                                     0x12e7a64, 0x12e7a6c, 0x12e7a7c};
    const uintptr_t *entries;
    if (table == base + 0x18c3130) {
        entries = disk_entries;
        puts("RESOURCE_FILE_KIND\tdisk");
    } else if (table == base + 0x18c35b0) {
        entries = apk_entries;
        puts("RESOURCE_FILE_KIND\tapk");
    } else {
        return 61;
    }
    for (size_t index = 0; index < sizeof(indices) / sizeof(*indices); ++index) {
        if (pointer_at(table + indices[index] * 8) != base + entries[index]) return 62;
    }
    puts("RESOURCE_FILE_VTABLE\t1");
    int valid = ((int (*)(void *))(base + entries[5]))(file);
    if (!valid) {
        resource_exercise(file, entries, 0, 0);
        puts("RESOURCE_INVALID_FILE\t1");
        puts("PHASE\tresource_close_enter");
        ((void (*)(void *))(base + entries[0]))(file);
        puts("PHASE\tresource_close_return");
        puts("RESOURCE_MISSING\t1");
        return 60;
    }
    int64_t length = ((int64_t (*)(void *))(base + entries[4]))(file);
    printf("RESOURCE_VALID_LENGTH\t%d\t%" PRId64 "\n", valid, length);
    if (!valid || length < 1 || length > 1024 * 1024) return 63;
    unsigned char *bytes = malloc((size_t)length);
    if (!bytes) return 64;
    int64_t total = 0;
    while (total < length) {
        int64_t amount = ((int64_t (*)(void *, void *, int))(base + entries[1]))(
            file, bytes + total, (int)(length - total));
        printf("RESOURCE_READ\t%" PRId64 "\n", amount);
        if (amount < 1 || amount > length - total) return 65;
        total += amount;
    }
    unsigned char extra = 0;
    int64_t eof = ((int64_t (*)(void *, void *, int))(base + entries[1]))(file, &extra, 1);
    int64_t end = ((int64_t (*)(void *))(base + entries[3]))(file);
    int at_end = ((int (*)(void *))(base + entries[6]))(file);
    int64_t seek_result = ((int64_t (*)(void *, int64_t))(base + entries[2]))(file, 0);
    printf("RESOURCE_SEEK_RESULT\t%" PRId64 "\n", seek_result);
    int64_t beginning = ((int64_t (*)(void *))(base + entries[3]))(file);
    int64_t reread = ((int64_t (*)(void *, void *, int))(base + entries[1]))(file, &extra, 1);
    printf("RESOURCE_SEMANTICS\t%" PRId64 "\t%" PRId64 "\t%d\t%" PRId64 "\t%" PRId64 "\t%d\n",
           eof, end, at_end, beginning, reread, extra == bytes[0]);
    if (getenv("NASHURA_RESOURCE_EXERCISE")) {
        if (((int64_t (*)(void *, int64_t))(base + entries[2]))(file, 0) != 0) return 66;
        resource_exercise(file, entries, valid, length);
    }
    puts("PHASE\tresource_close_enter");
    ((void (*)(void *))(base + entries[0]))(file);
    puts("PHASE\tresource_close_return");
    if (seek_result != 0 || eof != 0 || end != length || at_end != 1 || beginning != 0 || reread != 1 || extra != bytes[0]) return 66;
    FILE *output = fopen("resource.bin", "wbx");
    if (!output || fwrite(bytes, 1, (size_t)length, output) != (size_t)length || fclose(output)) return 67;
    free(bytes);
    printf("RESOURCE_BYTES\t%" PRId64 "\n", total);
    return 0;
}

static int preload_catalog(uintptr_t context, uintptr_t reader, uintptr_t list) {
    if (!guard("NASHURA_LISTENER_CTOR_GUARD", 0x1252638) ||
        !guard("NASHURA_LISTENER_DTOR_GUARD", 0x12526c0) ||
        !guard("NASHURA_PRELOAD_GUARD", 0xe16a08)) return 96;
    struct {
        uint64_t before[2];
        unsigned char object[0x28];
        uint64_t after[2];
    } listener;
    memset(&listener, 0xa5, sizeof(listener));
    listener.before[0] = listener.before[1] = canary;
    listener.after[0] = listener.after[1] = canary;
    puts("PHASE\tlistener_constructor_enter");
    ((void (*)(void *))(base + 0x1252638))(listener.object);
    puts("PHASE\tlistener_constructor_return");
    uintptr_t table = pointer_at((uintptr_t)listener.object);
    if (table != base + 0x18c0bb8 ||
        pointer_at(table + 0x20) != base + 0x12529a4 ||
        pointer_at(table + 0x38) != base + 0x1252ac0) return 97;
    printf("PHASE\tcatalog_preload_enter\tTID\t%ld\n", syscall(SYS_gettid));
    ((void (*)(void *, void *, void *))(base + 0xe16a08))(
        (void *)reader, (void *)list, listener.object);
    puts("PHASE\tcatalog_preload_return");
    int32_t state;
    memcpy(&state, listener.object + 0x20, sizeof(state));
    uintptr_t begin = pointer_at((uintptr_t)listener.object + 8);
    uintptr_t end = pointer_at((uintptr_t)listener.object + 0x10);
    if (end < begin || (end - begin) % 0x30 || (end - begin) / 0x30 > 16384) return 98;
    printf("CATALOG_PRELOAD\t%d\t%zu\n", state, (end - begin) / 0x30);
    puts("PHASE\tlistener_destructor_enter");
    ((void (*)(void *))(base + 0x12526c0))(listener.object);
    puts("PHASE\tlistener_destructor_return");
    int intact = listener.before[0] == canary && listener.before[1] == canary &&
                 listener.after[0] == canary && listener.after[1] == canary;
    printf("LISTENER_CANARY\t%d\n", intact);
    printf("CATALOG_READY_AFTER_PRELOAD\t%u\n", *(unsigned char *)(context + 0x6e8));
    return intact && state == 3 ? 0 : 99;
}

static int load_catalog(uintptr_t context, uintptr_t list, int32_t count) {
    if (!guard("NASHURA_TABLE_LOAD_GUARD", 0xe1bdcc)) return 100;
    int32_t batch = count / 4;
    if (batch < 1) return 101;
    for (int32_t start = 0; start < count; start += batch) {
        printf("PHASE\tcatalog_load_enter\t%d\t%d\tTID\t%ld\n", start, batch, syscall(SYS_gettid));
        int result = ((int (*)(void *, void *, int, int, int, int, int))(base + 0xe1bdcc))(
            (void *)context, (void *)list, 0, 0, 0, start, batch);
        printf("CATALOG_LOAD_RETURN\t%d\t%d\t%u\n", start, result,
               *(unsigned char *)(context + 0x6e8));
        if (result != (start + batch >= count)) return 102;
    }
    puts("CATALOG_TABLE_LOAD_COMPLETE\t1");
    return 0;
}

static int inspect_parsed_tables(uintptr_t context, const char *phase) {
    static const unsigned int observed_slots[] = {9, 10, 22, 34, 35, 123, 133, 166, 173,
                                                   179, 187, 189, 191, 205, 212, 213, 214};
    if (!guard("NASHURA_CONTEXT_GET_GUARD", 0xdccb88) ||
        !guard("NASHURA_TABLE_CTOR_GUARD", 0xdc76ac) ||
        !guard("NASHURA_CSV_GET_GUARD", 0x12101d4) ||
        !is_readable(context, 219 * sizeof(uintptr_t))) return 107;
    unsigned int populated = 0;
    uint64_t total_rows = 0;
    for (size_t index = 0; index < 219; ++index) {
        uintptr_t object = pointer_at(context + index * sizeof(uintptr_t));
        if (!object) continue;
        if (!is_readable(object, 0x90)) return 108;
        int32_t capacity, count, type;
        memcpy(&capacity, (void *)(object + 0x38), 4);
        memcpy(&count, (void *)(object + 0x3c), 4);
        memcpy(&type, (void *)(object + 0x84), 4);
        uintptr_t owner = pointer_at(object + 0x78);
        uintptr_t rows = pointer_at(object + 0x30);
        if (type != (int32_t)index || owner != context || count < 0 || count > 1000000 ||
            capacity < count || capacity > 1000000 ||
            (count && !is_readable(rows, (size_t)count * sizeof(uintptr_t)))) return 109;
        unsigned int nonnull_rows = 0;
        int inspect_ids = 0;
        for (size_t slot = 0; slot < sizeof(observed_slots) / sizeof(observed_slots[0]); ++slot)
            if (index == observed_slots[slot]) inspect_ids = 1;
        for (int32_t row = 0; row < count; ++row) {
            uintptr_t value;
            memcpy(&value, (void *)(rows + (size_t)row * sizeof(uintptr_t)), sizeof(value));
            nonnull_rows += value != 0;
            if (value && inspect_ids && getenv("NASHURA_INSPECT_ROW_IDS") && !strcmp(phase, "after")) {
                if (!is_readable(value, 0x44)) return 112;
                uint32_t id;
                memcpy(&id, (void *)(value + 0x40), sizeof(id));
                printf("PARSED_ROW_ID\t%zu\t%d\t%" PRIu32 "\n", index, row, id);
            }
        }
        int32_t csv_rows = -1, csv_columns = -1;
        uintptr_t csv = pointer_at(object + 0x88);
        if (csv) {
            if (!is_readable(csv, 0x58)) return 110;
            memcpy(&csv_rows, (void *)(csv + 0x54), 4);
            memcpy(&csv_columns, (void *)(csv + 0x34), 4);
            if (csv_rows < 0 || csv_rows > 1000000 || csv_columns < 0 || csv_columns > 100000) return 111;
        }
        printf("PARSED_TABLE\t%s\t%zu\t%d\t%d\t%u\t%d\t%d\n",
               phase, index, count, capacity, nonnull_rows, csv_rows, csv_columns);
        ++populated;
        total_rows += (uint64_t)count;
    }
    printf("PARSED_TABLE_SUMMARY\t%s\t%u\t%" PRIu64 "\n", phase, populated, total_rows);
    return 0;
}

static int run_catalog_loader(uintptr_t context, uintptr_t list, int construct_only) {
    if (!guard("NASHURA_LOADER_CTOR_GUARD", 0xc828d8) ||
        !guard("NASHURA_LOADER_START_GUARD", 0xc833ec) ||
        !guard("NASHURA_LOADER_STEP_GUARD", 0xc8346c) ||
        !guard("NASHURA_LOADER_DTOR_GUARD", 0xc8eb64)) return 103;
    struct {
        uint64_t before[2];
        unsigned char object[0xb8];
        uint64_t after[2];
    } loader;
    memset(&loader, 0xa5, sizeof(loader));
    loader.before[0] = loader.before[1] = canary;
    loader.after[0] = loader.after[1] = canary;
    puts("PHASE\tloader_constructor_enter");
    ((void (*)(void *, void *, void *))(base + 0xc828d8))(
        loader.object, (void *)context, (void *)list);
    puts("PHASE\tloader_constructor_return");
    if (pointer_at((uintptr_t)loader.object) != context ||
        pointer_at((uintptr_t)loader.object + 8) != list) return 104;
    unsigned int iterations = 0;
    if (getenv("NASHURA_INSPECT_PARSED_TABLES")) {
        int status = inspect_parsed_tables(context, "before");
        if (status) return status;
    }
    if (!construct_only) {
        puts("PHASE\tloader_start_enter");
        ((void (*)(void *))(base + 0xc833ec))(loader.object);
        puts("PHASE\tloader_start_return");
    }
    for (; !construct_only && iterations < 4096 && !*(unsigned char *)(context + 0x6e8); ++iterations) {
        int32_t state;
        memcpy(&state, loader.object + 0x10, sizeof(state));
        printf("LOADER_STEP_ENTER\t%u\t%d\n", iterations, state);
        int result = ((int (*)(void *))(base + 0xc8346c))(loader.object);
        memcpy(&state, loader.object + 0x10, sizeof(state));
        printf("LOADER_STEP_RETURN\t%u\t%d\t%d\t%u\n", iterations, state, result,
               *(unsigned char *)(context + 0x6e8));
        if (result != 1 && !*(unsigned char *)(context + 0x6e8)) return 105;
    }
    if (getenv("NASHURA_INSPECT_PARSED_TABLES")) {
        int status = inspect_parsed_tables(context, "after");
        if (status) return status;
    }
    puts("PHASE\tloader_destructor_enter");
    ((void (*)(void *))(base + 0xc8eb64))(loader.object);
    puts("PHASE\tloader_destructor_return");
    int intact = loader.before[0] == canary && loader.before[1] == canary &&
                 loader.after[0] == canary && loader.after[1] == canary;
    printf("LOADER_CANARY\t%d\n", intact);
    printf("CATALOG_READY_AFTER_LOADER\t%u\n", *(unsigned char *)(context + 0x6e8));
    if (construct_only) return intact && !*(unsigned char *)(context + 0x6e8) ? 0 : 106;
    return intact && iterations < 4096 && *(unsigned char *)(context + 0x6e8) == 1 ? 0 : 106;
}

static int inspect_catalog(uintptr_t context) {
    const char *mode = getenv("NASHURA_CATALOG_MODE");
    if (!mode) return 0;
    if (strcmp(mode, "inspect") && strcmp(mode, "list") && strcmp(mode, "preload") &&
        strcmp(mode, "load") && strcmp(mode, "loader") && strcmp(mode, "loader-construct")) return 72;
    uintptr_t reader = base + 0x19c3c10;
    uintptr_t table = pointer_at(reader);
    printf("CATALOG_READER\t%" PRIxPTR "\t%d\n", table ? table - base : 0,
           table && is_readable(table, 0x40));
    if (table && is_readable(table, 0x40)) {
        for (size_t index = 0; index < 8; ++index) {
            uintptr_t entry = pointer_at(table + index * 8);
            printf("CATALOG_READER_METHOD\t%zu\t%" PRIxPTR "\n", index, entry ? entry - base : 0);
        }
    }
    printf("CATALOG_READY_AFTER_INSPECT\t%u\n", *(unsigned char *)(context + 0x6e8));
    if (strcmp(mode, "inspect")) {
        if (!guard("NASHURA_LIST_GUARD", 0xe16fe4) || table != base + 0x1887140) return 73;
        const uintptr_t methods[] = {0xe16afc, 0xe16b00, 0xe16a08, 0xe169f8,
                                     0xe16a5c, 0xe16ab8, 0xe16a00};
        for (size_t index = 0; index < sizeof(methods) / sizeof(*methods); ++index) {
            if (pointer_at(table + index * 8) != base + methods[index]) return 74;
        }
        printf("PHASE\tcatalog_list_enter\tTID\t%ld\n", syscall(SYS_gettid));
        uintptr_t list = ((uintptr_t (*)(void))(base + 0xe16fe4))();
        puts("PHASE\tcatalog_list_return");
        if (!is_readable(list, 16)) return 75;
        int32_t capacity, count;
        memcpy(&capacity, (void *)(list + 8), sizeof(capacity));
        memcpy(&count, (void *)(list + 12), sizeof(count));
        uintptr_t data = pointer_at(list);
        printf("CATALOG_LIST\t%d\t%d\n", capacity, count);
        if (count < 1 || count > 4096 || capacity < count || capacity > 4096 ||
            !is_readable(data, (size_t)count * sizeof(uintptr_t))) return 76;
        for (int32_t index = 0; index < count; ++index) {
            if (!is_readable(pointer_at(data + (size_t)index * 8), 32)) return 77;
        }
        printf("CATALOG_READY_AFTER_LIST\t%u\n", *(unsigned char *)(context + 0x6e8));
        if (!strcmp(mode, "preload") || !strcmp(mode, "load") || !strcmp(mode, "loader") ||
            !strcmp(mode, "loader-construct")) {
            int status = preload_catalog(context, reader, list);
            if (status) return status;
        }
        if (!strcmp(mode, "load")) {
            int status = load_catalog(context, list, count);
            if (status) return status;
        }
        if (!strcmp(mode, "loader") || !strcmp(mode, "loader-construct")) {
            int status = run_catalog_loader(context, list, !strcmp(mode, "loader-construct"));
            if (status) return status;
        }
        puts("CATALOG_LIST_OWNERSHIP\tprocess_exit_only");
    }
    return 0;
}

static int prepare_factory(void) {
    const char *mode = getenv("NASHURA_FACTORY_MODE");
    if (!mode) return 0;
    if (strcmp(mode, "construct") && strcmp(mode, "register")) return 80;
    if (!guard("NASHURA_FACTORY_CTOR_GUARD", 0x1194a40) ||
        !guard("NASHURA_FACTORY_SET_GUARD", 0x11919d4) ||
        !guard("NASHURA_NEW_GUARD", 0x17e4b20)) return 81;
    uintptr_t factory_slot = pointer_at(base + 0x19f1228);
    uintptr_t manager_slot = pointer_at(base + 0x19f3988);
    printf("FACTORY_INITIAL_SLOTS\t%" PRIxPTR "\t%" PRIxPTR "\n", factory_slot, manager_slot);
    if (factory_slot || manager_slot) return 82;
    if (!is_readable(base + 0x1a06a50, 16)) return 83;
    unsigned char cache_path[16];
    memcpy(cache_path, (void *)(base + 0x1a06a50), sizeof(cache_path));
    int32_t cache_hash, cache_length;
    memcpy(&cache_hash, cache_path, 4);
    memcpy(&cache_length, cache_path + 4, 4);
    printf("FACTORY_CACHE_STRING\t%d\t%d\t%u\n", cache_hash, cache_length, cache_path[8]);
    if (cache_length != 0 || cache_path[8] != 0) return 84;
    if (!guard("NASHURA_PACKED_SET_GUARD", 0x119597c) ||
        !guard("NASHURA_PACKED_GET_GUARD", 0x1195988) ||
        !guard("NASHURA_PACKED_CALLER_GUARD", 0x756c94)) return 93;
    int packed_before = ((int (*)(void))(base + 0x1195988))();
    printf("PACKED_READER_BEFORE\t%d\n", packed_before);
    if (packed_before != 0) return 94;
    puts("PHASE\tpacked_reader_initialize_enter");
    ((void (*)(int))(base + 0x119597c))(1);
    puts("PHASE\tpacked_reader_initialize_return");
    int packed_after = ((int (*)(void))(base + 0x1195988))();
    printf("PACKED_READER_AFTER\t%d\n", packed_after);
    if (packed_after != 1) return 95;
    unsigned char *factory = ((void *(*)(size_t))(base + 0x17e4b20))(24);
    if (!factory) return 85;
    memcpy(factory + 8, &canary, 8);
    memcpy(factory + 16, &canary, 8);
    puts("PHASE\tfactory_constructor_enter");
    ((void (*)(void *))(base + 0x1194a40))(factory);
    puts("PHASE\tfactory_constructor_return");
    uintptr_t table = pointer_at((uintptr_t)factory);
    if (table != base + 0x18ba4f8 || memcmp(factory + 8, &canary, 8) ||
        memcmp(factory + 16, &canary, 8)) return 86;
    for (size_t index = 0; index < 3; ++index)
        printf("FACTORY_METHOD\t%zu\t%" PRIxPTR "\n", index, pointer_at(table + index * 8) - base);
    puts("FACTORY_CANARY\t1");
    if (!strcmp(mode, "construct")) {
        ((void (*)(void *))(base + 0x17e4b50))(factory);
        puts("FACTORY_ALLOCATION_RELEASED\t1");
        return 0;
    }
    const unsigned char flags[2] = {1, 0};
    puts("FACTORY_REGISTER_ARGUMENTS\tempty_root_original_caller\t1\t0");
    printf("PHASE\tfactory_register_enter\tTID\t%ld\n", syscall(SYS_gettid));
    ((void (*)(void *, const char *, const void *))(base + 0x11919d4))(factory, "", flags);
    puts("PHASE\tfactory_register_return");
    factory_slot = pointer_at(base + 0x19f1228);
    manager_slot = pointer_at(base + 0x19f3988);
    printf("FACTORY_FINAL_SLOTS\t%d\t%d\n", factory_slot == (uintptr_t)factory,
           manager_slot != 0 && is_readable(manager_slot, 0x1d8));
    if (factory_slot != (uintptr_t)factory || !manager_slot ||
        !is_readable(manager_slot, 0x1d8) || memcmp(factory + 8, &canary, 8) ||
        memcmp(factory + 16, &canary, 8)) return 87;
    puts("FACTORY_OWNERSHIP\tnative_global_process_exit_only");
    return 0;
}

int native_provider_probe(uintptr_t engine, int (*readable)(uintptr_t, size_t)) {
    base = engine;
    is_readable = readable;
    const char *mode = getenv("NASHURA_PROVIDER_MODE");
    const char *data = getenv("NASHURA_DATA_PATH");
    const char *update = getenv("NASHURA_UPDATE_PATH");
    if (!mode || !data || !update ||
        (strcmp(mode, "data") && strcmp(mode, "update") && strcmp(mode, "both"))) return 50;
    if (!guard("NASHURA_STRING_GUARD", 0x767f3c) ||
        !guard("NASHURA_DELETE_GUARD", 0x17e4b50) ||
        !guard("NASHURA_CLEAR_GUARD", 0x12e157c) ||
        !guard("NASHURA_DATA_GUARD", 0x12e166c) ||
        !guard("NASHURA_UPDATE_GUARD", 0x12e1714)) return 51;
    uintptr_t manager = pointer_at(base + 0x19f8d50);
    if (!manager || !is_readable(manager, 0x188)) return 52;
    puts("PHASE\tprovider_clear_enter");
    ((void (*)(void *))(base + 0x12e157c))((void *)manager);
    puts("PHASE\tprovider_clear_return");
    const char *paths[] = {data, update};
    const char *names[] = {"data", "update"};
    size_t offsets[] = {0x12e166c, 0x12e1714};
    size_t slots[] = {0x160, 0x170};
    for (size_t index = 0; index < 2; ++index) {
        if (strcmp(mode, "both") && strcmp(mode, names[index])) continue;
        GuardedString value;
        if (!string_create(&value, paths[index])) return 53;
        printf("PHASE\tregister_enter\t%s\tTID\t%ld\n", names[index], syscall(SYS_gettid));
        ((void (*)(void *, void *))(base + offsets[index]))((void *)manager, value.object);
        printf("PHASE\tregister_return\t%s\n", names[index]);
        if (!string_valid(&value) || !describe(manager, slots[index], names[index])) return 54;
        string_release(&value);
    }
    const char *disk_namespace = getenv("NASHURA_DISK_NAMESPACE");
    if (disk_namespace) {
        if (strcmp(disk_namespace, "data:") && strcmp(disk_namespace, "asset:")) return 68;
        if (!guard("NASHURA_GENERIC_GUARD", 0x12e24ec)) return 69;
        GuardedString path, prefix;
        if (!string_create(&path, data) || !string_create(&prefix, disk_namespace)) return 70;
        printf("PHASE\tgeneric_register_enter\t%s\n", disk_namespace);
        ((void (*)(void *, void *, void *))(base + 0x12e24ec))((void *)manager, path.object, prefix.object);
        printf("PHASE\tgeneric_register_return\t%s\n", disk_namespace);
        if (!string_valid(&path) || !string_valid(&prefix)) return 71;
        string_release(&path);
        string_release(&prefix);
    }
    if (getenv("NASHURA_SEARCH_PROVIDER")) {
        if (!guard("NASHURA_SEARCH_GUARD", 0x12e26dc)) return 88;
        const char *search_root = getenv("NASHURA_SEARCH_PATH");
        if (!search_root) search_root = data;
        GuardedString search_path, search_prefix;
        if (!string_create(&search_path, search_root) || !string_create(&search_prefix, "data:")) return 89;
        puts("PHASE\tsearch_provider_register_enter");
        ((void (*)(void *, void *, void *))(base + 0x12e26dc))((void *)manager, search_path.object, search_prefix.object);
        puts("PHASE\tsearch_provider_register_return");
        if (!string_valid(&search_path) || !string_valid(&search_prefix)) return 90;
        string_release(&search_path);
        string_release(&search_prefix);
        uintptr_t search_begin = pointer_at(manager + 0x118);
        uintptr_t search_end = pointer_at(manager + 0x120);
        if (!search_begin || search_end != search_begin + 16 || !is_readable(search_begin, 16)) return 91;
        printf("SEARCH_PROVIDER_COUNT\t%zu\n", (search_end - search_begin) / 16);
        uintptr_t search_object = pointer_at(search_begin);
        if (!search_object || pointer_at(search_object) != base + 0x18c3400) return 92;
    }
    uintptr_t root = pointer_at(base + 0x19ad878);
    uintptr_t context = pointer_at(root + 0x20);
    if (!is_readable(context + 0x6e8, 1)) return 55;
    printf("PROVIDER_CONTENT_READY\t%u\n", *(unsigned char *)(context + 0x6e8));
    int resource_status = 0;
    if (getenv("NASHURA_ALLOCATOR_ONLY")) {
        if (!exercise_allocator()) return 99;
        goto release_provider;
    }
    if (getenv("NASHURA_OWNER_GETTER_ONLY")) {
        if (getenv("NASHURA_APP_VERSION") && !install_app_version()) return 98;
        if (!guard("NASHURA_OWNER_GETTER_GUARD", 0x7557d0) ||
            !is_readable(base + 0x1997cf0, sizeof(uintptr_t))) return 96;
        printf("OWNER_SLOT_BEFORE_NONNULL\t%d\n", pointer_at(base + 0x1997cf0) != 0);
        puts("PHASE\towner_getter_enter");
        uintptr_t owner = (uintptr_t)((void *(*)(void))(base + 0x7557d0))();
        puts("PHASE\towner_getter_return");
        int readable = is_readable(owner, 0x530);
        printf("OWNER_GETTER_RESULT\t%d\t%d\t%d\n", owner != 0, readable,
               owner != 0 && owner == pointer_at(base + 0x1997cf0));
        if (!owner || !readable) return 97;
        printf("OWNER_VTABLE_MATCH\t%d\n", pointer_at(owner) == base + 0x17ea568);
        goto release_provider;
    }
    int factory_status = prepare_factory();
    if (factory_status) return factory_status;
    resource_status = read_resource(manager);
    if (resource_status && resource_status != 60) return resource_status;
    if (!resource_status) {
        int catalog_status = inspect_catalog(context);
        if (catalog_status) return catalog_status;
    }
release_provider:
    puts("PHASE\tprovider_release_enter");
    ((void (*)(void *))(base + 0x12e157c))((void *)manager);
    puts("PHASE\tprovider_release_return");
    if (pointer_at(manager + 0x160) || pointer_at(manager + 0x170)) return 56;
    puts("PROVIDER_SLOTS_RELEASED\t1");
    puts("PROVIDER_COVERAGE\tregistration_and_optional_catalog_attempt");
    return resource_status;
}
