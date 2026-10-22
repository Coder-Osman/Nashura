// Drive original battle methods; only orchestration and diagnostics live here.
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern void call_sret2(void *fn, void *result, const void *arg0, const void *arg1);

static void stream_bool(void *stream, size_t slot, int value) {
    uintptr_t *vtable = *(uintptr_t **)stream;
    ((void (*)(void *, int))vtable[slot / sizeof(uintptr_t)])(stream, value);
}

void *battle_probe_snapshot_wrapper(uintptr_t base, void *manager) {
    void *service = ((void *(*)(void))(base + 0xc8c5b8))();
    const char *mode = getenv("CR_SNAPSHOT_MODE");
    if (mode && strcmp(mode, "original") == 0) {
        if (!service) return NULL;
        void *wrapper = NULL;
        call_sret2((void *)(base + 0xad9a50), &wrapper, manager, NULL);
        return wrapper;
    }
    void *context = NULL;
    if (service) memcpy(&context, (char *)service + 0x48, sizeof(context));
    void *wrapper = ((void *(*)(size_t))(base + 0x17e4b20))(0x90);
    if (!wrapper) return NULL;
    ((void (*)(void *))(base + 0x105e870))(wrapper);
    void *stream = ((void *(*)(void *))(base + 0x121bd78))(wrapper);
    void *alternate = NULL;
    memcpy(&alternate, (char *)manager + 0x218, sizeof(alternate));
    // Follow ad9a50's stream prologue and call its original serializers.
    // Missing service means a diagnostic null context, pending Android comparison.
    stream_bool(stream, 0x78, 0);
    stream_bool(stream, 0x78, alternate != NULL);
    ((void (*)(void *))(base + 0x12141bc))(stream);
    stream_bool(stream, 0x40, 1);
    ((void (*)(void *, void *, int, void *, int))(base + 0x10632b0))(
        manager, stream, 1, context, 0);
    ((void (*)(void *, void *, int, void *, int, int, int))(base + 0x1062c3c))(
        manager, stream, 1, context, 0, 0, 0);
    return wrapper;
}

static void *member(uintptr_t base, void *root, const char *key) {
    _Alignas(16) unsigned char name[24];
    ((void (*)(void *, const char *))(base + 0x138a810))(name, key);
    void *entry = ((void *(*)(void *, void *, int))(base + 0xd0e100))(
        (char *)root + 0x18, name, 1);
    ((void (*)(void *))(base + 0x138a700))(name);
    void *value = NULL;
    if (entry) memcpy(&value, (char *)entry + 0x20, sizeof(value));
    return value;
}

int battle_probe_id_diagnostics(uintptr_t base, char *output, size_t capacity) {
    void *context = ((void *(*)(void))(base + 0x80031c))();
    if (!context) return 0;
    void *registry = NULL;
    memcpy(&registry, (char *)context + 0xad8, sizeof(registry));
    if (!registry) return 0;
    uintptr_t *methods = *(uintptr_t **)registry;
    size_t map_size = 0;
    memcpy(&map_size, (char *)registry + 0x48, sizeof(map_size));
    int used = snprintf(output, capacity,
        "{\"ok\":true,\"event\":\"id_registry\",\"vtable\":\"0x%lx\","
        "\"load_method\":\"0x%lx\",\"map_size\":%zu,\"towers\":[",
        (unsigned long)((uintptr_t)methods - base),
        (unsigned long)(methods[3] - base), map_size);
    if (used < 0 || (size_t)used >= capacity) return 0;
    const int32_t ids[] = {35000000, 35000001};
    for (unsigned i = 0; i < sizeof(ids) / sizeof(ids[0]); ++i) {
        void *data = ((void *(*)(void *, int))(base + 0xdd1340))(context, ids[i]);
        if (!data) return 0;
        int32_t stored_id = 0, mapped_id = 0;
        memcpy(&stored_id, (char *)data + 0x40, sizeof(stored_id));
        void *kind = ((void *(*)(void *))(*(uintptr_t **)data)[14])(data);
        void *name = ((void *(*)(void *))(base + 0xdc25ec))(data);
        _Alignas(16) unsigned char key[24];
        call_sret2((void *)(base + 0x138b080), key, kind, name);
        void *entry = ((void *(*)(void *, void *))(base + 0xc9f138))(
            (char *)registry + 0x30, key);
        if (entry) memcpy(&mapped_id, (char *)entry + 0x20, sizeof(mapped_id));
        int32_t key_length;
        memcpy(&key_length, key + 4, sizeof(key_length));
        const char *key_text = key_length < 8 ? (char *)key + 8 : *(const char **)(key + 8);
        int n = snprintf(output + used, capacity - (size_t)used,
            "%s{\"requested_id\":%d,\"stored_id\":%d,\"mapped_id\":%d,"
            "\"key\":\"%.*s\"}", i ? "," : "", ids[i], stored_id, mapped_id,
            key_length >= 0 && key_length < 240 ? key_length : 0, key_text);
        ((void (*)(void *))(base + 0x138a700))(key);
        if (n < 0 || (size_t)n >= capacity - (size_t)used) return 0;
        used += n;
    }
    return snprintf(output + used, capacity - (size_t)used, "]}") == 2;
}

void battle_probe_report_tower_lookup(uintptr_t base, const char *stage) {
    char output[2048];
    printf("{\"event\":\"id_registry_stage\",\"stage\":\"%s\"}\n", stage);
    if (battle_probe_id_diagnostics(base, output, sizeof(output))) puts(output);
}

int battle_probe_player_fields_json(void *manager, int tick, char *output, size_t capacity) {
    void *world = NULL;
    memcpy(&world, (char *)manager + 0xa8, sizeof(world));
    if (!world) return 0;
    size_t used = 0;
    int n = snprintf(output, capacity, "{\"event\":\"player_fields\",\"tick\":%d,\"players\":[", tick);
    if (n < 0 || (size_t)n >= capacity) return 0;
    used += (size_t)n;
    for (int owner = 0; owner < 2; ++owner) {
        void *player = NULL;
        memcpy(&player, (char *)world + 0xe0 + owner * sizeof(player), sizeof(player));
        if (!player) return 0;
        n = snprintf(output + used, capacity - used, "%s[", owner ? "," : "");
        if (n < 0 || (size_t)n >= capacity - used) return 0;
        used += (size_t)n;
        for (int offset = 0x2e0; offset <= 0x318; offset += 4) {
            int32_t value = 0;
            memcpy(&value, (char *)player + offset, sizeof(value));
            n = snprintf(output + used, capacity - used, "%s%d", offset == 0x2e0 ? "" : ",", value);
            if (n < 0 || (size_t)n >= capacity - used) return 0;
            used += (size_t)n;
        }
        n = snprintf(output + used, capacity - used, "]");
        if (n < 0 || (size_t)n >= capacity - used) return 0;
        used += (size_t)n;
    }
    return snprintf(output + used, capacity - used, "]}") == 2;
}

int battle_probe_tower_bytes_json(void *manager, int tick, int slot, char *output, size_t capacity) {
    void *world = NULL;
    memcpy(&world, (char *)manager + 0xa8, sizeof(world));
    if (!world) return 0;
    void *player = NULL;
    memcpy(&player, (char *)world + 0xe0, sizeof(player));
    if (!player) return 0;
    void *object_manager = NULL;
    memcpy(&object_manager, (char *)player + 0x10, sizeof(object_manager));
    if (!object_manager) return 0;
    void **objects = NULL;
    int32_t count = 0;
    memcpy(&objects, (char *)object_manager + 0x08, sizeof(objects));
    memcpy(&count, (char *)object_manager + 0x14, sizeof(count));
    if (!objects || count < 6 || slot < 0 || slot >= 6 || !objects[slot]) return 0;
    const unsigned char *object = objects[slot];
    char hex[0x120 * 2 + 1];
    for (int j = 0; j < 0x120; ++j) {
        static const char digits[] = "0123456789abcdef";
        unsigned char byte = object[0x70 + j];
        hex[j * 2] = digits[byte >> 4];
        hex[j * 2 + 1] = digits[byte & 15];
    }
    hex[sizeof(hex) - 1] = '\0';
    int n = snprintf(output, capacity,
                     "{\"event\":\"tower_bytes\",\"tick\":%d,\"slot\":%d,\"offset\":112,\"hex\":\"%s\"}",
                     tick, slot, hex);
    return n > 0 && (size_t)n < capacity;
}

int battle_probe_tower_component_json(void *manager, int tick, int slot, int component_slot,
                                      char *output, size_t capacity) {
    void *world = NULL;
    memcpy(&world, (char *)manager + 0xa8, sizeof(world));
    if (!world) return 0;
    void *player = NULL;
    memcpy(&player, (char *)world + 0xe0, sizeof(player));
    if (!player) return 0;
    void *object_manager = NULL;
    memcpy(&object_manager, (char *)player + 0x10, sizeof(object_manager));
    if (!object_manager) return 0;
    void **objects = NULL;
    int32_t count = 0;
    memcpy(&objects, (char *)object_manager + 0x08, sizeof(objects));
    memcpy(&count, (char *)object_manager + 0x14, sizeof(count));
    if (!objects || count < 6 || slot < 0 || slot >= 6 || !objects[slot]) return 0;
    void *object = objects[slot];
    void **components = NULL;
    int32_t component_count = 0;
    memcpy(&components, (char *)object + 0x18, sizeof(components));
    memcpy(&component_count, (char *)object + 0x24, sizeof(component_count));
    if (!components || component_count != 4 || component_slot < 0 || component_slot >= 4 ||
        !components[component_slot]) return 0;
    const unsigned char *bytes = components[component_slot];
    const int sizes[] = {0xc0, 0, 0x30, 0x38};
    int size = sizes[component_slot];
    if (!size) return 0;
    char hex[0xc0 * 2 + 1];
    static const char digits[] = "0123456789abcdef";
    for (int j = 0; j < size; ++j) {
        hex[j * 2] = digits[bytes[j] >> 4];
        hex[j * 2 + 1] = digits[bytes[j] & 15];
    }
    hex[size * 2] = '\0';
    int n = snprintf(output, capacity,
                     "{\"event\":\"tower_component\",\"tick\":%d,\"slot\":%d,"
                     "\"component\":%d,\"hex\":\"%s\"}", tick, slot, component_slot, hex);
    return n > 0 && (size_t)n < capacity;
}

static void battle_probe_report_tower_components(void *manager, int tick) {
    char output[1024];
    for (int slot = 0; slot < 6; ++slot) {
        for (int component = 0; component < 4; ++component) {
            if (battle_probe_tower_component_json(manager, tick, slot, component,
                                                   output, sizeof(output))) puts(output);
        }
    }
}

static void battle_probe_report_tower_bytes(void *manager, int tick) {
    char output[1024];
    for (int slot = 0; slot < 6; ++slot) {
        if (battle_probe_tower_bytes_json(manager, tick, slot, output, sizeof(output))) puts(output);
    }
}

static void battle_probe_report_player_fields(void *manager, int tick) {
    char output[1024];
    if (battle_probe_player_fields_json(manager, tick, output, sizeof(output))) puts(output);
}

static void report_tower_data(uintptr_t base, void *manager) {
    void *world = NULL;
    memcpy(&world, (char *)manager + 0xa8, sizeof(world));
    if (!world) return;
    void *player = NULL;
    memcpy(&player, (char *)world + 0xe0, sizeof(player));
    if (!player) return;
    void *object_manager = NULL;
    memcpy(&object_manager, (char *)player + 0x10, sizeof(object_manager));
    if (!object_manager) return;
    void **objects = NULL;
    int32_t count = 0;
    memcpy(&objects, (char *)object_manager + 0x08, sizeof(objects));
    memcpy(&count, (char *)object_manager + 0x14, sizeof(count));
    if (!objects || count < 0 || count > 10000) return;
    printf("{\"event\":\"battle_object_count\",\"count\":%d}\n", count);
    for (int32_t i = 0; i < count && i < 8; ++i) {
        void *object = objects[i];
        if (!object) continue;
        void *data = NULL;
        int32_t native_id = 0, data_id = 0;
        memcpy(&native_id, (char *)object + 0x08, sizeof(native_id));
        memcpy(&data, (char *)object + 0x48, sizeof(data));
        if (data) memcpy(&data_id, (char *)data + 0x40, sizeof(data_id));
        printf("{\"event\":\"battle_object_data\",\"slot\":%d,\"native_id\":%d,"
               "\"data_id\":%d,\"data_present\":%s}\n",
               i, native_id, data_id, data ? "true" : "false");
    }
    battle_probe_report_tower_lookup(base, "after_battle_load");
}

static int snapshot(uintptr_t base, void *manager, int tick, const char *directory) {
    void *wrapper = battle_probe_snapshot_wrapper(base, manager);
    if (!wrapper) return 0;
    int32_t size = 0;
    const unsigned char *bytes = NULL;
    memcpy(&size, (char *)wrapper + 0x1c, sizeof(size));
    memcpy(&bytes, (char *)wrapper + 0x40, sizeof(bytes));
    int valid = size > 0 && size < 32 * 1024 * 1024 && bytes;
    uint64_t digest = UINT64_C(1469598103934665603);
    if (valid) {
        for (int i = 0; i < size; ++i) {
            digest ^= bytes[i];
            digest *= UINT64_C(1099511628211);
        }
        if (directory) {
            char path[4096];
            int length = snprintf(path, sizeof(path), "%s/tick-%06d.bin", directory, tick);
            if (length < 0 || length >= (int)sizeof(path)) valid = 0;
            else {
                FILE *out = fopen(path, "wb");
                if (!out) valid = 0;
                else {
                    if (fwrite(bytes, 1, (size_t)size, out) != (size_t)size) valid = 0;
                    if (fclose(out) != 0) valid = 0;
                }
            }
        }
    }
    printf("{\"event\":\"battle_snapshot\",\"tick\":%d,\"bytes\":%d,"
           "\"fnv64\":\"%016llx\",\"valid\":%s}\n",
           tick, size, (unsigned long long)digest, valid ? "true" : "false");
    ((void (*)(void *))(base + 0x105e8bc))(wrapper);
    return valid;
}

static int inject_command(uintptr_t base, void *manager, const char *text) {
    _Alignas(16) unsigned char string[24];
    ((void (*)(void *, const char *))(base + 0x138a810))(string, text);
    void *json = ((void *(*)(void *, void *, int))(base + 0x121a648))(string, NULL, 100);
    ((void (*)(void *))(base + 0x138a700))(string);
    if (!json) return 0;
    void *context = NULL, *queue = NULL, *command = NULL;
    memcpy(&context, (char *)manager + 0x208, sizeof(context));
    memcpy(&queue, (char *)manager + 0x38, sizeof(queue));
    if (context && queue) call_sret2((void *)(base + 0xd2fe78), &command, json, context);
    ((void (*)(void *))(*(uintptr_t **)json)[1])(json);
    if (!command) return 0;
    ((void (*)(void *, void **))(base + 0xd2eef0))(queue, &command);
    return 1;
}

// Shared harness for both hosts. Action lines are injection_tick TAB native JSON.
int battle_probe_trace(uintptr_t base, void *manager, int steps, const char *actions,
                       const char *directory, void (*step_fn)(void *), char *report, size_t capacity) {
    if (!manager || steps <= 0 || steps > 10000 || !directory || !step_fn) return 39;
    FILE *input = actions ? fopen(actions, "rb") : NULL;
    if (actions && !input) return 41;
    char line[4096];
    int action_tick = -1, last_action_tick = -1, injected = 0;
    char *command = NULL;
    int (*tick_fn)(void *) = (void *)(base + 0x1062bc0);
    int tick = tick_fn(manager), start_tick = tick, stagnant = 0, snapshots = 0, error = 0;
    if (!snapshot(base, manager, tick, directory)) error = 38;
    else snapshots++;
    for (int i = 0; !error && i < steps; ++i) {
        while (input) {
            if (!command) {
                if (!fgets(line, sizeof(line), input)) {
                    if (ferror(input)) error = 42;
                    break;
                }
                char *end = NULL;
                long value = strtol(line, &end, 10);
                if (end == line || *end != '\t' || value < tick || value < last_action_tick || value > 10000 ||
                    !strchr(end, '\n')) { error = 43; break; }
                action_tick = (int)value;
                last_action_tick = action_tick;
                command = end + 1;
            }
            if (action_tick > tick) break;
            if (action_tick != tick || !inject_command(base, manager, command)) { error = 44; break; }
            injected++;
            printf("{\"event\":\"command_injected\",\"tick\":%d,\"index\":%d}\n", tick, injected);
            command = NULL;
        }
        if (error) break;
        step_fn(manager);
        int next = tick_fn(manager);
        if (next == tick) {
            if (++stagnant >= 4) break;
            continue;
        }
        if (next != tick + 1) { error = 40; break; }
        stagnant = 0;
        tick = next;
        if (!snapshot(base, manager, tick, directory)) { error = 38; break; }
        snapshots++;
        void *world = NULL;
        memcpy(&world, (char *)manager + 0xa8, sizeof(world));
        if (world && *((unsigned char *)world + 0x1e0)) break;
    }
    if (!error && input && !command) {
        if (fgetc(input) != EOF) error = 45;
        else if (ferror(input)) error = 42;
    }
    if (input) fclose(input);
    void *world = NULL, *queue = NULL;
    int result = -1, queue_count = -1, finalized = 0;
    memcpy(&world, (char *)manager + 0xa8, sizeof(world));
    memcpy(&queue, (char *)manager + 0x38, sizeof(queue));
    if (world) {
        memcpy(&result, (char *)world + 0x1b8, sizeof(result));
        finalized = *((unsigned char *)world + 0x1e0) != 0;
    }
    if (queue) memcpy(&queue_count, (char *)queue + 0x14, sizeof(queue_count));
    if (!error && command) error = 45;
    int n = snprintf(report, capacity,
        "{\"ok\":%s,\"event\":\"battle_trace_completed\",\"error_code\":%d,\"start_tick\":%d,"
        "\"tick\":%d,\"snapshots\":%d,\"injected\":%d,\"queue_count\":%d,"
        "\"finalized\":%s,\"world_result_raw\":%d,\"stagnant\":%d,\"snapshot_directory\":\"%s\"}",
        error ? "false" : "true", error, start_tick, tick, snapshots, injected, queue_count,
        finalized ? "true" : "false", result, stagnant, directory);
    if (n < 0 || (size_t)n >= capacity) return 46;
    return error;
}

int battle_probe_run(uintptr_t base) {
    const char *config = getenv("CR_BATTLE_CONFIG");
    if (!config) return 0;
    FILE *input = fopen(config, "rb");
    if (!input) return 30;
    char text[65536];
    size_t length = fread(text, 1, sizeof(text) - 1, input);
    int failed = ferror(input) || !feof(input);
    fclose(input);
    if (failed || length < 2) return 31;
    text[length] = '\0';
    _Alignas(16) unsigned char string[24];
    ((void (*)(void *, const char *))(base + 0x138a810))(string, text);
    void *root = ((void *(*)(void *, void *, int))(base + 0x121a648))(string, NULL, 100);
    ((void (*)(void *))(base + 0x138a700))(string);
    if (!root || !member(base, root, "battle")) return 32;
    const char *streams[] = {"cmd", "evt"};
    for (unsigned i = 0; i < 2; ++i) {
        void *array = member(base, root, streams[i]);
        int32_t count = -1;
        if (array) memcpy(&count, (char *)array + 0x14, sizeof(count));
        if (count != 0) return 33;
    }
    puts("{\"event\":\"battle_config_parsed\"}");
    void *context_a = ((void *(*)(void))(base + 0x80031c))();
    void *context_b = ((void *(*)(void))(base + 0xeaf1c8))();
    const char *location_text = getenv("CR_BATTLE_LOCATION");
    int location_id = location_text ? atoi(location_text) : 0;
    if (!context_a || !context_b || location_id <= 0) return 34;
    battle_probe_report_tower_lookup(base, "before_battle_load");
    void *location = ((void *(*)(void *, int))(base + 0xdd1340))(context_a, location_id);
    if (!location) return 35;
    void *tilemap = ((void *(*)(void *))(base + 0xdf8508))(location);
    if (!tilemap) {
        puts("{\"event\":\"before_original_tilemap_ensure\"}");
        ((void (*)(void *))(base + 0x857608))(location);
        tilemap = ((void *(*)(void *))(base + 0xdf8508))(location);
    }
    printf("{\"event\":\"battle_tilemap\",\"location\":%d,\"present\":%s}\n",
           location_id, tilemap ? "true" : "false");
    if (!tilemap) return 36;
    void *manager = ((void *(*)(size_t))(base + 0x17e4b20))(0x230);
    if (!manager) return 37;
    puts("{\"event\":\"before_original_battle_constructor\"}");
    ((void (*)(void *, int, void *, void *, int))(base + 0x1060ba8))(
        manager, 1, context_a, context_b, 0);
    puts("{\"event\":\"original_battle_constructor_returned\"}");
    ((void (*)(void *, int))(base + 0x1060f84))(manager, 1);
    puts("{\"event\":\"before_original_battle_load\"}");
    ((void (*)(void *, void *, void *, void *))(base + 0x1061b1c))(manager, root, NULL, NULL);
    int (*tick_fn)(void *) = (void *)(base + 0x1062bc0);
    int tick = tick_fn(manager);
    printf("{\"event\":\"original_battle_load_returned\",\"tick\":%d}\n", tick);
    report_tower_data(base, manager);
    battle_probe_report_player_fields(manager, tick);
    battle_probe_report_tower_bytes(manager, tick);
    battle_probe_report_tower_components(manager, tick);
    const char *steps_text = getenv("CR_BATTLE_STEPS");
    int steps = steps_text ? atoi(steps_text) : 20;
    char report[1024] = {};
    int error = battle_probe_trace(base, manager, steps, getenv("CR_BATTLE_ACTIONS"), getenv("CR_SNAPSHOT_DIR"),
                                  (void (*)(void *))(base + 0x10620a4), report, sizeof(report));
    puts(report);
    ((void (*)(void *))(base + 0x10610b0))(manager);
    ((void (*)(void *))(base + 0x988844))(manager);
    ((void (*)(void *))(base + 0x17e4b50))(manager);
    ((void (*)(void *))(*(uintptr_t **)root)[1])(root);
    puts("{\"event\":\"battle_owned_state_released\"}");
    return error;
}
