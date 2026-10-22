// Call the original SCDB constructor with real file bytes in an isolated host.
#define _GNU_SOURCE
#include <dlfcn.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>
#include "crash_trace.h"

extern void call_sret2(void *fn, void *result, const void *arg0, const void *arg1);
extern int battle_probe_run(uintptr_t base);
extern void battle_probe_report_tower_lookup(uintptr_t base, const char *stage);

static void dump_original_string(uintptr_t base, uintptr_t offset, const char *label) {
    const unsigned char *obj = (const unsigned char *)(base + offset);
    int32_t length;
    memcpy(&length, obj + 4, sizeof(length));
    const char *data = length < 8 ? (const char *)(obj + 8) : *(const char *const *)(obj + 8);
    if (!data || length < 0 || length > 240) {
        printf("{\"event\":\"global_string\",\"name\":\"%s\",\"length\":%d,\"present\":false}\n",
               label, length);
        return;
    }
    char value[241];
    memcpy(value, data, length);
    value[length] = '\0';
    printf("{\"event\":\"global_string\",\"name\":\"%s\",\"length\":%d,\"value\":\"",
           label, length);
    for (int32_t i = 0; i < length; ++i) {
        unsigned char c = (unsigned char)value[i];
        if (c == '\\' || c == '"') printf("\\%c", c);
        else if (c >= 0x20 && c < 0x7f) putchar((int)c);
        else printf("\\u%04x", c);
    }
    puts("\"}");
}

static void dump_original_asset_globals(uintptr_t base, const char *stage) {
    printf("{\"event\":\"asset_globals\",\"stage\":\"%s\"}\n", stage);
    dump_original_string(base, 0x01a06000 + 0xa40, "a40");
    dump_original_string(base, 0x01a06000 + 0xa50, "a50");
    dump_original_string(base, 0x01a06000 + 0xa60, "a60");
    dump_original_string(base, 0x01a06000 + 0xa70, "a70");
    dump_original_string(base, 0x01a06000 + 0xa80, "a80");
    dump_original_string(base, 0x01a06000 + 0xa90, "a90");
}

static void register_original_reader(uintptr_t base, const char *directory) {
    // Match GameMain at 0x756c84..0x756cc4 before its reader construction.
    ((void (*)(int))(base + 0x01182b34))(0);
    ((void (*)(int))(base + 0x0119597c))(1);
    const uintptr_t flag_slots[] = {0x018e58b8, 0x018e58c0, 0x018e58c8};
    for (unsigned i = 0; i < sizeof(flag_slots) / sizeof(flag_slots[0]); ++i) {
        unsigned char *flag = *(unsigned char **)(base + flag_slots[i]);
        if (!flag) _exit(13);
        *flag = 1;
    }
    puts("{\"event\":\"original_reader_prerequisites_applied\"}");
    // The derived constructor is inlined only at 0x756cc8..0x756ce4.
    // Reproduce that ABI construction exactly: original allocator, original
    // base constructor, then the original relocated derived vtable. Every
    // virtual method remains original libg code; there are no host callbacks.
    void *reader = ((void *(*)(size_t))(base + 0x017e4b20))(8);
    if (!reader) _exit(9);
    memset(reader, 0, 8);
    ((void (*)(void *))(base + 0x01194a40))(reader);
    uintptr_t vtable;
    memcpy(&vtable, (void *)(base + 0x018e58d0), sizeof(vtable));
    if (vtable != base + 0x018579a0) _exit(10);
    vtable += 16;
    memcpy(reader, &vtable, sizeof(vtable));
    (void)directory;
    // GameMain passes the C string inside its original 16-byte String,
    // not a libc++ std::string nor the String object itself.
    _Alignas(16) unsigned char namespace_string[24];
    call_sret2((void *)(base + 0x0139cc9c), namespace_string, NULL, NULL);
    int32_t namespace_length;
    memcpy(&namespace_length, namespace_string + 4, sizeof(namespace_length));
    const char *namespace_text = namespace_length < 8 ? (char *)namespace_string + 8 :
        *(const char **)(namespace_string + 8);
    const unsigned char config[2] = {1, 0}; // Original GameMain call at 0x756d04.
    puts("{\"event\":\"before_original_reader_registration\",\"offset\":\"0x11919d4\"}");
    ((void (*)(void *, const char *, const void *))(base + 0x011919d4))(
        reader, namespace_text, config);
    void *registered;
    memcpy(&registered, (void *)(base + 0x019f1228), sizeof(registered));
    printf("{\"event\":\"original_reader_registered\",\"identity_matches\":%s}\n",
           registered == reader ? "true" : "false");
    if (registered != reader) _exit(11);
    ((void (*)(void *))(base + 0x0138a700))(namespace_string);
}

static int report_content_context(uintptr_t base) {
    void *root = NULL, *context = NULL;
    unsigned char ready = 0;
    memcpy(&root, (void *)(base + 0x019ad878), sizeof(root));
    if (root) memcpy(&context, (char *)root + 0x20, sizeof(context));
    if (context) memcpy(&ready, (char *)context + 0x6e8, sizeof(ready));
    printf("{\"event\":\"content_context\",\"root_present\":%s,"
           "\"context_present\":%s,\"postprocess_ready\":%u}\n",
           root ? "true" : "false", context ? "true" : "false", ready);
    return root && context && ready == 1;
}

static void initialize_original_content_services(uintptr_t base) {
    // Original GameMain 0x756f24..0x756f8c, preserving its ownership transfers.
    _Alignas(16) unsigned char path[24];
    call_sret2((void *)(base + 0x0139cccc), path, NULL, NULL);
    ((void (*)(void *))(base + 0x0119189c))(path);
    ((void (*)(void *))(base + 0x0138a700))(path);
    puts("{\"event\":\"original_content_namespace_configured\"}");
    void *owner = ((void *(*)(size_t))(base + 0x017e4b20))(8);
    if (!owner) _exit(18);
    uintptr_t vtable = base + 0x017ea900;
    memcpy(owner, &vtable, sizeof(vtable));
    void *identity = owner;
    ((void (*)(void **))(base + 0x01190fdc))(&owner);
    if (owner || ((void *(*)(void))(base + 0x01190fd0))() != identity) _exit(19);
    puts("{\"event\":\"original_content_service_owner_transferred\"}");
    puts("{\"event\":\"headless_graphics_service_skipped\",\"offset\":\"0xb6211c\"}");
    const uintptr_t offsets[] = {0x0118804c, 0x00796288,
                                 0x00c81cec, 0x00b78550, 0x00ac857c};
    for (unsigned i = 0; i < sizeof(offsets) / sizeof(offsets[0]); ++i) {
        if (i == 0) ((void (*)(int))(base + 0x0135574c))(1);
        printf("{\"event\":\"before_original_content_service\",\"offset\":\"0x%lx\"}\n",
               (unsigned long)offsets[i]);
        ((void (*)(void))(base + offsets[i]))();
        printf("{\"event\":\"original_content_service_returned\",\"offset\":\"0x%lx\"}\n",
               (unsigned long)offsets[i]);
    }
}

static void configure_original_paths(uintptr_t base) {
    const char *root = getenv("CR_ENGINE_ROOT");
    if (!root || root[0] != '/') _exit(20);
    _Alignas(16) unsigned char path[24];
    ((void (*)(void *, const char *))(base + 0x0138a810))(path, root);
    ((void (*)(void *))(base + 0x0139ccf8))(path);
    ((void (*)(void *))(base + 0x0138a700))(path);
    dump_original_asset_globals(base, "after_original_path_configuration");
    dump_original_string(base, 0x019f1190, "resource_manifest");
}

static int preload_original_tables(uintptr_t base, void *list, int32_t count) {
    _Alignas(16) unsigned char request[48];
    ((void (*)(void *))(base + 0x01252638))(request);
    uintptr_t *methods = *(uintptr_t **)request;
    printf("{\"event\":\"resource_request_methods\",\"submit\":\"0x%lx\","
           "\"loader_prepare\":\"0x%lx\"}\n",
           (unsigned long)(methods[7] - base),
           (unsigned long)((*(uintptr_t **)(base + 0x019c3c10))[2] - base));
    if (methods[7] != base + 0x01252ac0 ||
        (*(uintptr_t **)(base + 0x019c3c10))[2] != base + 0x00e16a08) _exit(22);
    printf("{\"event\":\"before_original_table_preload\",\"tables\":%d}\n", count);
    ((void (*)(void *, void *, void *))(base + 0x00e16a08))(
        (void *)(base + 0x019c3c10), list, request);
    int32_t state;
    memcpy(&state, request + 32, sizeof(state));
    printf("{\"event\":\"original_table_preload_returned\",\"state\":%d}\n", state);
    ((void (*)(void *))(base + 0x012526c0))(request);
    return state == 3;
}

static int initialize_original_state_masks(uintptr_t base, void *context, void *list) {
    // Stock deferred loader c84370 uses context/list at +0/+8 and progress
    // floats at +0x34/+0x3c. Its original predicate selects the state table.
    _Alignas(16) unsigned char state[0x40] = {0};
    memcpy(state, &context, sizeof(context));
    memcpy(state + 8, &list, sizeof(list));
    void *stage[2] = {NULL, state};
    puts("{\"event\":\"before_original_state_mask_stage\",\"offset\":\"0xc84370\"}");
    int next = ((int (*)(void *))(base + 0x00c84370))(stage);
    const char *names[] = {"MOVING", "JUMPING", "DASHING", "CHARGING", "PATHFINDING", "ATTACKING"};
    printf("{\"event\":\"original_state_mask_stage_returned\",\"next\":%d,\"masks\":{", next);
    for (unsigned i = 0; i < sizeof(names) / sizeof(names[0]); ++i) {
        uint64_t value;
        memcpy(&value, (void *)(base + 0x019c9f20 + i * sizeof(value)), sizeof(value));
        printf("%s\"%s\":%llu", i ? "," : "", names[i], (unsigned long long)value);
    }
    puts("}}");
    return next == 3;
}

int resource_probe_run(int argc, char **argv) {
    if (argc < 3 || argc > 4) return 2;
    setvbuf(stdout, NULL, _IONBF, 0);
    alarm(120);
    void *h = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!h) { fprintf(stderr, "%s\n", dlerror()); return 3; }
    Dl_info info;
    if (!dladdr(dlsym(h, "JNI_OnLoad"), &info)) return 4;
    uintptr_t base = (uintptr_t)info.dli_fbase;
    install_fault_diagnostic(base);
    puts("{\"event\":\"before_original_filesystem_bootstrap\",\"offset\":\"0x12dfed4\"}");
    ((void (*)(void))(base + 0x012dfed4))();
    puts("{\"event\":\"original_filesystem_bootstrap_returned\"}");
    dump_original_asset_globals(base, "after_filesystem_bootstrap");
    configure_original_paths(base);
    const char *slash = strrchr(argv[2], '/');
    if (!slash) return 6;
    _Alignas(16) unsigned char directory[24];
    size_t directory_length = (size_t)(slash - argv[2]) + 1;
    char *directory_text = strndup(argv[2], directory_length);
    if (!directory_text) return 7;
    // The original constructor copies length+1 bytes, including the terminator.
    ((void (*)(void *, const char *, size_t))(base + 0x00767f3c))(
        directory, directory_text, directory_length);
    void *filesystem = ((void *(*)(void))(base + 0x012dff54))();
    // createFileSystem passes its object to this provider bootstrap before
    // retrieving the active filesystem again.
    ((void (*)(void *))(base + 0x012e157c))(filesystem);
    puts("{\"event\":\"original_provider_bootstrap_returned\"}");
    dump_original_asset_globals(base, "after_provider_bootstrap");
    filesystem = ((void *(*)(void))(base + 0x012dff54))();
    int apk_reader = argc == 4 && strcmp(argv[3], "--apk-reader-list") == 0;
    if (apk_reader) {
        _Alignas(16) unsigned char empty[24];
        ((void (*)(void *, const char *, size_t))(base + 0x00767f3c))(empty, "", 0);
        ((void (*)(void *, const void *))(base + 0x012e166c))(filesystem, empty);
        puts("{\"event\":\"original_apk_file_provider_registered\"}");
    }
    // Original disk overlay registration at libg+0x13a4080. The sibling
    // 0x12e166c registers APK assets and requires a real AAssetManager instead.
    ((void (*)(void *, const void *))(base + 0x012e1714))(filesystem, directory);
    puts("{\"event\":\"original_local_file_provider_registered\"}");
    // GameMain registers the reader before opening the SCDB. Keeping this
    // order matters: content bootstrap consults the reader registry while
    // resolving the first table.
    int reader_list = argc == 4 && (strcmp(argv[3], "--reader-list") == 0 || apk_reader);
    if (reader_list) register_original_reader(base, "");
    if (apk_reader) {
        // GameMain 0x756f0c..0x756f20; configuration reads use original JNI.
        ((void (*)(int))(base + 0x0139b220))(0);
        ((void (*)(void))(base + 0x00827ecc))();
        puts("{\"event\":\"before_original_preinit\",\"offset\":\"0x76cee4\"}");
        ((void (*)(void))(base + 0x0076cee4))();
        void *published = NULL;
        memcpy(&published, (void *)(base + 0x01998108), sizeof(published));
        printf("{\"event\":\"original_preinit_returned\",\"object_present\":%s}\n",
               published ? "true" : "false");
        if (!published) return 17;
        ((void (*)(void *, int))(base + 0x00757774))(published, 0);
        puts("{\"event\":\"original_preinit_configured\"}");
        initialize_original_content_services(base);
    }
    // Use the original std::string constructor; do not hand-build its state.
    _Alignas(16) unsigned char prefix[24];
    ((void (*)(void *, const char *, size_t))(base + 0x00767f3c))(prefix, "", 0);
    void *result[2] = {NULL, NULL};
    char resource_uri[512];
    if (snprintf(resource_uri, sizeof(resource_uri), "update:/%s", slash + 1) >= (int)sizeof(resource_uri)) return 8;
    puts("{\"event\":\"before_original_scdb_open\",\"offset\":\"0x11b4b30\"}");
    call_sret2((void *)(base + 0x011b4b30), result, prefix, resource_uri);
    printf("{\"event\":\"original_scdb_open_returned\",\"object_present\":%s,\"owner_present\":%s}\n",
           result[0] ? "true" : "false", result[1] ? "true" : "false");
    if (apk_reader) {
        void *root = NULL;
        memcpy(&root, (void *)(base + 0x019ad878), sizeof(root));
        if (root) return 15;
        puts("{\"event\":\"before_original_content_bootstrap\",\"offset\":\"0x8001a4\"}");
        ((void (*)(void))(base + 0x008001a4))();
        puts("{\"event\":\"original_content_bootstrap_returned\"}");
        report_content_context(base);
    }
    if (apk_reader) {
        const char *uris[] = {
            "assets.scdb",
            "csv_logic/globals.csv",
            "csv_logic/global_ids_oldformat.csv",
            "update:/assets.scdb",
            "update:/csv_logic/globals.csv",
            "asset:/csv_logic/globals.csv",
            "asset:/csv_logic/global_ids_oldformat.csv",
            "update:/csv_logic/global_ids_oldformat.csv"
        };
        for (unsigned i = 0; i < sizeof(uris) / sizeof(uris[0]); i++) {
            void *found = ((void *(*)(const char *, int))(base + 0x01191270))(uris[i], 0);
            printf("{\"event\":\"original_resource_lookup\",\"uri\":\"%s\",\"present\":%s}\n",
                   uris[i], found ? "true" : "false");
        }
    }
    void *list = NULL;
    int32_t count = -1;
    if (argc == 4 && (strcmp(argv[3], "--list") == 0 || reader_list) && result[0]) {
        const unsigned char *table_key = (const unsigned char *)(base + 0x019c3bf0);
        uintptr_t *loader_vtable = *(uintptr_t **)(base + 0x019c3c10);
        printf("{\"event\":\"data_loader_methods\",\"vtable\":\"0x%lx\","
               "\"prepare\":\"0x%lx\",\"lookup\":\"0x%lx\",\"open\":\"0x%lx\"}\n",
               (unsigned long)((uintptr_t)loader_vtable - base),
               (unsigned long)(loader_vtable[4] - base),
               (unsigned long)(loader_vtable[6] - base),
               (unsigned long)(loader_vtable[5] - base));
        printf("{\"event\":\"data_list_table_key_bytes\",\"hex\":\"");
        for (int i = 0; i < 24; ++i) printf("%02x", table_key[i]);
        puts("\"}");
        puts("{\"event\":\"before_original_data_list\",\"offset\":\"0xe16fe4\"}");
        list = ((void *(*)(void))(base + 0x00e16fe4))();
        if (list) memcpy(&count, (char *)list + 12, sizeof(count));
        printf("{\"event\":\"original_data_list_returned\",\"present\":%s,\"count\":%d}\n",
               list ? "true" : "false", count);
        if (!list || count <= 0) return 12;
    }
    if (apk_reader) {
        void *root = NULL, *context = NULL;
        memcpy(&root, (void *)(base + 0x019ad878), sizeof(root));
        if (root) memcpy(&context, (char *)root + 0x20, sizeof(context));
        if (!context || !list || count <= 0) return 14;
        if (!preload_original_tables(base, list, count)) return 21;
        void *globals = ((void *(*)(const char *, int))(base + 0x01191270))(
            "csv_logic/globals.csv", 0);
        printf("{\"event\":\"original_resource_lookup_after_preload\",\"present\":%s}\n",
               globals ? "true" : "false");
        int32_t batch = (count + 3) / 4;
        for (int32_t start = 0; start < count; start += batch) {
            printf("{\"event\":\"before_original_table_batch\",\"start\":%d,\"size\":%d}\n",
                   start, batch);
            int done = ((int (*)(void *, void *, int, int, int, int, int))(base + 0x00e1bdcc))(
                context, list, 0, 0, 0, start, batch);
            printf("{\"event\":\"original_table_batch_returned\",\"start\":%d,\"done\":%d}\n",
                   start, done);
            if ((done != 0) != (start + batch >= count)) return 16;
            if (getenv("CR_BATTLE_CONFIG"))
                battle_probe_report_tower_lookup(base, "after_table_batch");
        }
        if (getenv("CR_BATTLE_CONFIG")) {
            const char *uris[] = {"csv_logic/global_ids_oldformat.csv", "csv_logic/globalIdDb.csv"};
            for (unsigned i = 0; i < sizeof(uris) / sizeof(uris[0]); ++i) {
                void *found = ((void *(*)(const char *, int))(base + 0x01191270))(uris[i], 0);
                printf("{\"event\":\"id_resource_after_preload\",\"uri\":\"%s\",\"present\":%s}\n",
                       uris[i], found ? "true" : "false");
            }
        }
        // Preserve the deferred loader's state-mask -> IDs -> postprocess order.
        if (!initialize_original_state_masks(base, context, list)) return 23;
        puts("{\"event\":\"before_original_id_registration\",\"offset\":\"0xdccee0\"}");
        ((void (*)(void *, void *))(base + 0x00dccee0))(context, list);
        puts("{\"event\":\"original_id_registration_returned\"}");
        if (getenv("CR_BATTLE_CONFIG"))
            battle_probe_report_tower_lookup(base, "after_id_registration");
        puts("{\"event\":\"before_original_content_load\",\"offset\":\"0xdcd364\"}");
        ((void (*)(void *))(base + 0x00dcd364))(context);
        puts("{\"event\":\"original_content_load_returned\"}");
        if (getenv("CR_BATTLE_CONFIG"))
            battle_probe_report_tower_lookup(base, "after_content_load");
        if (!report_content_context(base)) return 14;
        int battle_status = battle_probe_run(base);
        if (battle_status) return battle_status;
    }
    // The whole short-lived process owns this native state, including resources.
    return result[0] && result[1] ? 0 : 5;
}

#ifndef RESOURCE_PROBE_LIBRARY
int main(int argc, char **argv) { _exit(resource_probe_run(argc, argv)); }
#endif
