/* Version-bound adapter to the original Mainloop task interface.
 * Only for libg SHA256 110aa2b5...59783 (checked by the Java launcher).
 * Enqueue mirrors 0x139ef14..0x139ef70, reusing engine allocation,
 * vector growth, locks and notification. Consumer: 0x130f714, slots +0x10/+8.
 * This supplies a task, not an engine object, context, or readiness value.
 */
#define _GNU_SOURCE
#include <jni.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <errno.h>
#include <fcntl.h>
#include <unistd.h>
#include <stdatomic.h>
#include <signal.h>
#include <sys/prctl.h>
#include <sys/syscall.h>
#include <sys/resource.h>
#include <time.h>
#include "headless_spatial_y_bound.h"
#include "headless_hot_getters.h"
#include "headless_native_phases.h"
#include "headless_thread_resources.h"
#include "headless_saturated_distance.h"
#include "headless_combined_hot_distance.h"

static uintptr_t base;
static _Atomic int destroyed;
static _Atomic int busy;
static int requested_mode, task_status, init_result;
static unsigned char *battle_json;
static size_t battle_json_size;
static uint64_t last_battle_digest;
static int32_t last_battle_bytes;

static uint64_t monotonic_ns(void) {
    struct timespec value;
    if (clock_gettime(CLOCK_MONOTONIC, &value)) return 0;
    return (uint64_t)value.tv_sec * UINT64_C(1000000000) + (uint64_t)value.tv_nsec;
}

static int compare_duration(const void *left, const void *right) {
    uint64_t a = *(const uint64_t *)left, b = *(const uint64_t *)right;
    return (a > b) - (a < b);
}

struct task { void (**vtable)(struct task *); };

static int find_engine(struct dl_phdr_info *info, size_t size, void *opaque) {
    (void)size;
    (void)opaque;
    const char *name = strrchr(info->dlpi_name, '/');
    if (name && !strcmp(name + 1, "libg.so")) base = info->dlpi_addr;
    return 0;
}

static int entry_guards(void) {
    static const struct { uintptr_t offset; unsigned char bytes[16]; } entries[] = {
        {0x130f074, {0xfd, 0x7b, 0xbe, 0xa9, 0xf3, 0x0b, 0x00, 0xf9, 0xfd, 0x03, 0x00, 0x91, 0x48, 0x37, 0x00, 0xb0}},
        {0x17e4b20, {0x30, 0x08, 0x00, 0xb0, 0x11, 0xaa, 0x41, 0xf9, 0x10, 0x42, 0x0d, 0x91, 0x20, 0x02, 0x1f, 0xd6}},
        {0x17e4b50, {0x30, 0x08, 0x00, 0xb0, 0x11, 0xb6, 0x41, 0xf9, 0x10, 0xa2, 0x0d, 0x91, 0x20, 0x02, 0x1f, 0xd6}},
        {0xa63a6c, {0xfd, 0x7b, 0xbc, 0xa9, 0xf7, 0x0b, 0x00, 0xf9, 0xf6, 0x57, 0x02, 0xa9, 0xf4, 0x4f, 0x03, 0xa9}},
        {0x17e4e40, {0x30, 0x08, 0x00, 0xb0, 0x11, 0x72, 0x42, 0xf9, 0x10, 0x82, 0x13, 0x91, 0x20, 0x02, 0x1f, 0xd6}},
        {0x17e4e60, {0x30, 0x08, 0x00, 0xb0, 0x11, 0x7a, 0x42, 0xf9, 0x10, 0xc2, 0x13, 0x91, 0x20, 0x02, 0x1f, 0xd6}},
        {0x17e50c0, {0x30, 0x08, 0x00, 0x90, 0x11, 0x12, 0x43, 0xf9, 0x10, 0x82, 0x18, 0x91, 0x20, 0x02, 0x1f, 0xd6}},
        {0x8001a4, {0xfd, 0x7b, 0xbd, 0xa9, 0xf5, 0x0b, 0x00, 0xf9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0x756be8, {0xff, 0x43, 0x03, 0xd1, 0xfd, 0x7b, 0x08, 0xa9, 0xf9, 0x4b, 0x00, 0xf9, 0xf8, 0x5f, 0x0a, 0xa9}},
        {0x119597c, {0xe8, 0x42, 0x00, 0x90, 0x00, 0x81, 0x06, 0x39, 0xc0, 0x03, 0x5f, 0xd6, 0xe8, 0x42, 0x00, 0x90}},
        {0x1194a40, {0x28, 0x39, 0x00, 0xd0, 0x08, 0xe1, 0x13, 0x91, 0x08, 0x00, 0x00, 0xf9, 0xc0, 0x03, 0x5f, 0xd6}},
        {0x11919d4, {0xff, 0xc3, 0x01, 0xd1, 0xfd, 0x7b, 0x03, 0xa9, 0xf7, 0x23, 0x00, 0xf9, 0xf6, 0x57, 0x05, 0xa9}},
        {0xe16fe4, {0xff, 0xc3, 0x00, 0xd1, 0xfd, 0x7b, 0x01, 0xa9, 0xf3, 0x13, 0x00, 0xf9, 0xfd, 0x43, 0x00, 0x91}},
        {0x1252638, {0x68, 0x33, 0x00, 0xd0, 0x08, 0xe1, 0x2e, 0x91, 0x1f, 0x7c, 0x01, 0xa9, 0x08, 0x7c, 0x00, 0xa9}},
        {0xe16a08, {0xfd, 0x7b, 0xbd, 0xa9, 0xf5, 0x0b, 0x00, 0xf9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0x12526c0, {0xfd, 0x7b, 0xbd, 0xa9, 0xf6, 0x57, 0x01, 0xa9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0xc828d8, {0xff, 0xc3, 0x02, 0xd1, 0xfd, 0x7b, 0x05, 0xa9, 0xfc, 0x6f, 0x06, 0xa9, 0xfa, 0x67, 0x07, 0xa9}},
        {0xc833ec, {0xff, 0xc3, 0x00, 0xd1, 0xfd, 0x7b, 0x01, 0xa9, 0xf3, 0x13, 0x00, 0xf9, 0xfd, 0x43, 0x00, 0x91}},
        {0xc8346c, {0xff, 0xc3, 0x03, 0xd1, 0xfd, 0x7b, 0x09, 0xa9, 0xfc, 0x6f, 0x0a, 0xa9, 0xfa, 0x67, 0x0b, 0xa9}},
        {0xc8eb64, {0xfd, 0x7b, 0xbd, 0xa9, 0xf6, 0x57, 0x01, 0xa9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0x80031c, {0xfd, 0x7b, 0xbe, 0xa9, 0xf3, 0x0b, 0x00, 0xf9, 0xfd, 0x03, 0x00, 0x91, 0x73, 0x8d, 0x00, 0xb0}},
        {0x800368, {0xfd, 0x7b, 0xbe, 0xa9, 0xf3, 0x0b, 0x00, 0xf9, 0xfd, 0x03, 0x00, 0x91, 0x73, 0x8d, 0x00, 0xb0}},
        {0xeaf1c8, {0xe0, 0x51, 0x00, 0xf0, 0x00, 0xc0, 0x36, 0x91, 0xc0, 0x03, 0x5f, 0xd6, 0xe0, 0x03, 0x01, 0xaa}},
        {0x121a648, {0x80, 0x04, 0x00, 0xb4, 0x08, 0x04, 0x40, 0xb9, 0x28, 0x04, 0x00, 0x34, 0xfd, 0x7b, 0xbd, 0xa9}},
        {0xd0e100, {0xfd, 0x7b, 0xbc, 0xa9, 0xf7, 0x0b, 0x00, 0xf9, 0xf6, 0x57, 0x02, 0xa9, 0xf4, 0x4f, 0x03, 0xa9}},
        {0xdd1340, {0xfd, 0x7b, 0xbe, 0xa9, 0xf4, 0x4f, 0x01, 0xa9, 0xfd, 0x03, 0x00, 0x91, 0xf4, 0x03, 0x00, 0xaa}},
        {0xdf8508, {0xfd, 0x7b, 0xbe, 0xa9, 0xf4, 0x4f, 0x01, 0xa9, 0xfd, 0x03, 0x00, 0x91, 0xf3, 0x03, 0x00, 0xaa}},
        {0x857608, {0xfd, 0x7b, 0xbd, 0xa9, 0xf6, 0x57, 0x01, 0xa9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0x1060ba8, {0xfd, 0x7b, 0xbb, 0xa9, 0xfa, 0x67, 0x01, 0xa9, 0xf8, 0x5f, 0x02, 0xa9, 0xf6, 0x57, 0x03, 0xa9}},
        {0x1060f84, {0xfd, 0x7b, 0xbd, 0xa9, 0xf5, 0x0b, 0x00, 0xf9, 0xf4, 0x4f, 0x02, 0xa9, 0xfd, 0x03, 0x00, 0x91}},
        {0x1061b1c, {0xff, 0x83, 0x01, 0xd1, 0xfd, 0x7b, 0x01, 0xa9, 0xf9, 0x13, 0x00, 0xf9, 0xf8, 0x5f, 0x03, 0xa9}},
        {0x1062bc0, {0x00, 0x60, 0x40, 0xb9, 0xc0, 0x03, 0x5f, 0xd6, 0x00, 0x24, 0x40, 0xb9, 0xc0, 0x03, 0x5f, 0xd6}},
        {0x10620a4, {0xfd, 0x7b, 0xbc, 0xa9, 0xf8, 0x5f, 0x01, 0xa9, 0xf6, 0x57, 0x02, 0xa9, 0xf4, 0x4f, 0x03, 0xa9}},
        {0x10610b0, {0xfd, 0x7b, 0xbb, 0xa9, 0xf9, 0x0b, 0x00, 0xf9, 0xf8, 0x5f, 0x02, 0xa9, 0xf6, 0x57, 0x03, 0xa9}},
        {0x988844, {0xfd, 0x7b, 0xbe, 0xa9, 0xf4, 0x4f, 0x01, 0xa9, 0xfd, 0x03, 0x00, 0x91, 0xf3, 0x03, 0x00, 0xaa}},
    };
    for (size_t index = 0; index < sizeof(entries) / sizeof(entries[0]); ++index) {
        if (memcmp((const void *)(base + entries[index].offset), entries[index].bytes, 16)) return 0;
    }
    return 1;
}

static int read_pointer(int fd, uintptr_t address, uintptr_t *value) {
    return address && pread(fd, value, sizeof(*value),
        (off_t)(address & UINT64_C(0x00ffffffffffffff))) == sizeof(*value);
}

static uintptr_t pointer_at(uintptr_t address) {
    uintptr_t value = 0;
    memcpy(&value, (const void *)address, sizeof(value));
    return value;
}

/* Read-only, version-bound relationships emitted by original 0x8001a4.
 * The singleton's identity is observed; its full ResourceManager semantics
 * and the complete owner initializer are not inferred from this graph.
 */
static int inspect_content_identity(const char *phase) {
    static uintptr_t first_root, first_context, first_wrapper, first_singleton;
    uintptr_t root = 0, root_vtable = 0, context = 0, wrapper = 0;
    uintptr_t singleton = 0, singleton_slot = 0, singleton_vtable = 0, context_wrapper = 0;
    int fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
    int readable = fd >= 0
        && read_pointer(fd, base + 0x19ad878, &root)
        && read_pointer(fd, root, &root_vtable)
        && read_pointer(fd, root + 0x20, &context)
        && read_pointer(fd, root + 0x28, &wrapper)
        && context && wrapper
        && read_pointer(fd, wrapper, &singleton)
        && read_pointer(fd, base + 0x19f23c0, &singleton_slot)
        && singleton && read_pointer(fd, singleton, &singleton_vtable)
        && read_pointer(fd, context + 0xb60, &context_wrapper);
    if (fd >= 0) close(fd);
    int graph = readable && root_vtable == base + 0x17f3040
        && singleton == singleton_slot && singleton_vtable == base + 0x18bae08
        && context_wrapper == wrapper;
    int getter = graph && ((uintptr_t (*)(void))(base + 0x800368))() == wrapper;
    if (graph && getter && !first_root) {
        first_root = root; first_context = context;
        first_wrapper = wrapper; first_singleton = singleton;
    }
    int stable = first_root && root == first_root && context == first_context
        && wrapper == first_wrapper && singleton == first_singleton;
    printf("HEADLESS_CONTENT_IDENTITY phase=%s readable=%d root_vtable=%d singleton_vtable=%d singleton_slot=%d context_wrapper=%d wrapper_getter=%d stable=%d verified=%d\n",
           phase, readable, root_vtable == base + 0x17f3040,
           singleton_vtable == base + 0x18bae08, singleton != 0 && singleton == singleton_slot,
           wrapper != 0 && context_wrapper == wrapper, getter, stable, graph && getter && stable);
    fflush(stdout);
    return graph && getter && stable;
}

struct libg_string {
    int32_t cached_unicode_length;
    int32_t byte_length;
    union { char inline_bytes[8]; const char *pointer; } storage;
};

typedef void *(*json_parse_fn)(void *, void *, int32_t);
typedef void *(*json_member_find_fn)(void *, void *, int32_t);
typedef void *(*logic_data_by_id_fn)(void *, int32_t);
typedef int32_t (*logic_data_type_index_fn)(int32_t);
typedef void *(*tilemap_lookup_fn)(void *);
typedef void (*tilemap_ensure_fn)(void *);
typedef void *(*game_alloc_fn)(size_t);
typedef void (*game_delete_fn)(void *);
typedef void (*game_constructor_fn)(void *, int32_t, void *, void *, int32_t);
typedef void (*game_setup_fn)(void *, int32_t);
typedef void (*game_load_fn)(void *, void *, void *, void *);
typedef void (*game_step_fn)(void *);
typedef int32_t (*game_tick_fn)(void *);
typedef void (*game_destroy_fn)(void *);
typedef void (*logic_command_append_fn)(void *, void **);
typedef void (*state_snapshot_delete_fn)(void *);
typedef void (*byte_stream_reset_fn)(void *);
typedef int32_t (*byte_stream_read_fn)(void *);
typedef void (*game_state_deserialize_header_fn)(void *, void *);
typedef void (*game_state_deserialize_body_fn)(void *, void *, int32_t, void *);

/* These two libg entry points return their object through AArch64 x8.  The
 * assembly bridge keeps the C caller ABI explicit and version independent. */
extern void cr_call_logic_command_parse(void *, void *, void **, void *);
extern void cr_call_state_snapshot(void *, void **, void *);
extern void cr_call_card_selection_build(void *, void *, int32_t, void *, void *);

static int init_libg_string(const char *text, struct libg_string *result) {
    size_t length;
    if (!text || !result) return 0;
    length = strlen(text);
    if (length > INT32_MAX) return 0;
    memset(result, 0, sizeof(*result));
    result->cached_unicode_length = -1;
    result->byte_length = (int32_t)length;
    if (length < sizeof(result->storage.inline_bytes)) {
        memcpy(result->storage.inline_bytes, text, length);
        result->storage.inline_bytes[length] = '\0';
    } else {
        result->storage.pointer = text;
    }
    return 1;
}

static void *parse_json_text(const char *text) {
    struct libg_string input;
    if (!init_libg_string(text, &input)) return NULL;
    return ((json_parse_fn)(base + 0x121a648))(&input, NULL, 100);
}

static void *lookup_json_member(void *object, const char *key) {
    struct libg_string name;
    void *entry, *value = NULL;
    if (!object || !init_libg_string(key, &name)) return NULL;
    entry = ((json_member_find_fn)(base + 0xd0e100))((unsigned char *)object + 0x18, &name, 1);
    if (entry) memcpy(&value, (unsigned char *)entry + 0x20, sizeof(value));
    return value;
}

static void release_json_object(void *object) {
    uintptr_t *vtable;
    if (!object) return;
    memcpy(&vtable, object, sizeof(vtable));
    if (vtable && vtable[1]) ((void (*)(void *))vtable[1])(object);
}

static int inspect_json_array(void *array, int32_t *count_out) {
    uintptr_t data = 0;
    int32_t capacity = -1, count = -1;
    if (!array) return 0;
    memcpy(&data, (unsigned char *)array + 0x08, sizeof(data));
    memcpy(&capacity, (unsigned char *)array + 0x10, sizeof(capacity));
    memcpy(&count, (unsigned char *)array + 0x14, sizeof(count));
    if (capacity < 0 || count < 0 || count > capacity || capacity > 100000 || (count && !data)) return 0;
    if (count_out) *count_out = count;
    return 1;
}

static int inspect_command_array(void *array, void ***data_out, int32_t *count_out) {
    uintptr_t data = 0;
    int32_t capacity = -1, count = -1;
    if (!array) return 0;
    memcpy(&data, (unsigned char *)array + 0x08, sizeof(data));
    memcpy(&capacity, (unsigned char *)array + 0x10, sizeof(capacity));
    memcpy(&count, (unsigned char *)array + 0x14, sizeof(count));
    if (capacity < 0 || count < 0 || count > capacity || capacity > 100000 ||
        (count && !data)) return 0;
    if (data_out) *data_out = (void **)data;
    if (count_out) *count_out = count;
    return 1;
}

static int32_t queue_count(void *queue) {
    int32_t count = -1;
    if (!queue) return count;
    memcpy(&count, (unsigned char *)queue + 0x14, sizeof(count));
    return count;
}

struct phase_metrics {
    int enabled;
    uint64_t battle_json_parse_ns;
    uint64_t game_state_load_ns;
    uint64_t logic_command_parse_ns;
    uint64_t logic_command_append_ns;
    int32_t loaded_command_count;
    int32_t injected_command_count;
    int32_t queue_at_step_start;
    int32_t queue_at_end;
    int32_t queue_removed;
    int32_t queue_added;
    int32_t queue_change_steps;
    int32_t first_queue_change_tick;
    int32_t last_queue_change_tick;
};

static int phase_timing_requested(void) {
    const char *value = getenv("NASHURA_PHASE_TIMING");
    return value && !strcmp(value, "1");
}

static int minimal_benchmark_requested(void) {
    const char *value = getenv("NASHURA_BENCHMARK_MINIMAL");
    return value && !strcmp(value, "1");
}

static int32_t i32_at(uintptr_t address) {
    int32_t value;
    memcpy(&value, (const void *)address, sizeof(value));
    return value;
}

/* Read-only, version-bound fields shared with the existing native replay probe.
 * No inferred winner or action success is produced from a tick increment. */
static int observe_battle(void *state, const char *phase) {
    uintptr_t world = pointer_at((uintptr_t)state + 0xa8);
    uintptr_t player = world ? pointer_at(world + 0xe0) : 0;
    uintptr_t objects = player ? pointer_at(player + 0x10) : 0;
    int32_t count = objects ? i32_at(objects + 0x14) : -1;
    int32_t capacity = objects ? i32_at(objects + 0x10) : -1;
    uintptr_t data = objects ? pointer_at(objects + 8) : 0;
    if (!world || count < 0 || capacity < count || capacity > 100000 || (count && !data)) return -1;
    int32_t tick = ((game_tick_fn)(base + 0x1062bc0))(state);
    unsigned char finalized = *(unsigned char *)(world + 0x1e0);
    if (finalized > 1) return -1;
    if (getenv("NASHURA_QUIET")) return finalized;
    printf("HEADLESS_BATTLE_OBSERVATION phase=%s tick=%d objects=%d queue=%d finalized=%u result_raw=%d\n",
           phase, tick, count, queue_count((void *)pointer_at((uintptr_t)state + 0x38)),
           finalized, i32_at(world + 0x1b8));
    for (int owner = 0; owner < 2; ++owner) {
        uintptr_t p = pointer_at(world + 0xe0 + (uintptr_t)owner * 8);
        if (!p) continue;
        int32_t hand_count = i32_at(p + 0x22c);
        uintptr_t hand = pointer_at(p + 0x220);
        printf("HEADLESS_BATTLE_PLAYER tick=%d owner=%d elixir_raw=%d hand_count=%d",
               tick, owner, i32_at(p + 0x2f8), hand_count);
        if (hand && hand_count >= 0 && hand_count <= 16)
            for (int32_t i = 0; i < hand_count; ++i) printf(" hand_%d=%d", i, i32_at(hand + (uintptr_t)i * 4));
        putchar('\n');
    }
    for (int32_t i = 0; i < count && i < 12; ++i) {
        uintptr_t object = pointer_at(data + (uintptr_t)i * 8);
        if (!object) continue;
        uintptr_t object_data = pointer_at(object + 0x48);
        uintptr_t components = pointer_at(object + 0x18);
        int32_t component_count = i32_at(object + 0x24);
        int32_t component_capacity = i32_at(object + 0x20);
        uintptr_t hp = components && component_count > 2 && component_capacity >= component_count
            && component_capacity < 1024 ? pointer_at(components + 16) : 0;
        if (hp && pointer_at(hp + 8) != object) hp = 0;
        printf("HEADLESS_BATTLE_OBJECT tick=%d slot=%d id=%d data_id=%d owner=%d x=%d y=%d hp=%d max_hp=%d\n",
               tick, i, i32_at(object + 8), object_data ? i32_at(object_data + 0x40) : -1,
               i32_at(object + 0x78), i32_at(object + 0x7c), i32_at(object + 0x80),
               hp ? i32_at(hp + 0x10) : -1, hp ? i32_at(hp + 0x14) : -1);
    }
    fflush(stdout);
    return finalized;
}

static int parse_and_append_commands(void *manager, void *commands, void *queue,
                                     struct phase_metrics *metrics) {
    void **items = NULL;
    int32_t count = -1;
    if (!manager || !commands || !queue || !inspect_command_array(commands, &items, &count)) return -40;
    printf("HEADLESS_GAME_STATE_COMMANDS_BEFORE count=%d queue_count=%d\n", count, queue_count(queue));
    fflush(stdout);
    void *context = NULL;
    memcpy(&context, (unsigned char *)manager + 0x208, sizeof(context));
    if (!context) return -41;
    for (int32_t index = 0; index < count; ++index) {
        void *json_command = NULL;
        memcpy(&json_command, (unsigned char *)items + (size_t)index * sizeof(json_command),
               sizeof(json_command));
        if (!json_command) return -42;
        void *logic_command = NULL;
        uint64_t started = metrics && metrics->enabled ? monotonic_ns() : 0;
        cr_call_logic_command_parse(json_command, context, &logic_command,
                                    (void *)(base + 0xd2fe78));
        if (metrics && metrics->enabled)
            metrics->logic_command_parse_ns += monotonic_ns() - started;
        printf("HEADLESS_GAME_STATE_COMMAND_PARSE index=%d json=%p result=%p\n",
               index, json_command, logic_command);
        fflush(stdout);
        if (!logic_command) return -43;
        int32_t queue_before = queue_count(queue);
        started = metrics && metrics->enabled ? monotonic_ns() : 0;
        ((logic_command_append_fn)(base + 0xd2eef0))(queue, &logic_command);
        if (metrics && metrics->enabled)
            metrics->logic_command_append_ns += monotonic_ns() - started;
        int32_t queue_after = queue_count(queue);
        printf("HEADLESS_GAME_STATE_COMMAND_APPEND index=%d owner_after=%p queue_before=%d queue_after=%d\n",
               index, logic_command, queue_before, queue_after);
        fflush(stdout);
        /* The stock append consumes the owner semantically, but this build's
         * ABI leaves the caller's temporary pointer unchanged.  Queue growth
         * is the authoritative acceptance signal; requiring a null temporary
         * pointer rejected a command that was already enqueued. */
        if (queue_before < 0 || queue_after != queue_before + 1) return -44;
    }
    return 1;
}

/* Queue stock commands using the original selection builder. Application and
 * validation stay in libg. The fixture must supply complete eight-card decks. */
static int queue_hand_deploy(void *state, int owner) {
    struct selection { void *card, *secondary; uint32_t packed, padding; } selection = {0};
    uintptr_t world = pointer_at((uintptr_t)state + 0xa8);
    uintptr_t player = world ? pointer_at(world + 0xe0 + (uintptr_t)owner * 8) : 0;
    uintptr_t identity = world ? pointer_at(world + 0x30 + (uintptr_t)owner * 8) : 0;
    uintptr_t deck = world ? pointer_at(world + 0x88 + (uintptr_t)owner * 8) : 0;
    if (!player || !identity || !deck) return -1;
    uintptr_t hand = pointer_at(player + 0x220), slots = pointer_at(deck + 0x20);
    int32_t hand_count = i32_at(player + 0x22c), slot_count = i32_at(deck + 0x2c);
    if (!hand || !slots || hand_count < 1 || hand_count > 16 || slot_count != 8) return -1;
    int32_t slot = i32_at(hand), elixir = i32_at(player + 0x2f8);
    if (slot < 0 || slot >= slot_count) return 0;
    uintptr_t deck_slot = pointer_at(slots + (uintptr_t)slot * 8);
    uintptr_t card = deck_slot ? pointer_at(deck_slot + 0x10) : 0;
    if (!card) return -1;
    cr_call_card_selection_build((void *)deck_slot, (void *)player, 0, &selection,
                                 (void *)(base + 0xe2d1bc));
    int encoded_slot = (int)((selection.packed >> 22) & 0x3f) - 1;
    if (selection.card != (void *)card || encoded_slot != slot) return -1;
    int cost = (int)(selection.packed >> 28);
    if (elixir < cost * 10000) return 0;
    uintptr_t effective = (selection.packed & 15) == 0 && selection.secondary
        ? (uintptr_t)selection.secondary : card;
    int32_t card_id = i32_at(effective + 0x40);
    int32_t tick = ((game_tick_fn)(base + 0x1062bc0))(state);
    char text[512];
    int n = snprintf(text, sizeof(text),
        "{\"ct\":86,\"c\":{\"t\":%d,\"t2\":%d,\"idHi\":%u,\"idLo\":%u,\"px\":3500,\"py\":%d,\"sid\":-1,\"sel\":{\"os\":%d,\"pd\":%u}}}",
        tick, tick + 20, (uint32_t)i32_at(identity), (uint32_t)i32_at(identity + 4),
        owner == 0 ? 12000 : 20000, card_id, selection.packed);
    if (n < 0 || n >= (int)sizeof(text)) return -1;
    void *json = parse_json_text(text), *command = NULL;
    void *context = (void *)pointer_at((uintptr_t)state + 0x208);
    void *queue = (void *)pointer_at((uintptr_t)state + 0x38);
    if (!json || !context || !queue) { release_json_object(json); return -1; }
    cr_call_logic_command_parse(json, context, &command, (void *)(base + 0xd2fe78));
    if (!command) { release_json_object(json); return -1; }
    int before = queue_count(queue);
    ((logic_command_append_fn)(base + 0xd2eef0))(queue, &command);
    int after = queue_count(queue);
    printf("HEADLESS_BATTLE_DEPLOY_QUEUED tick=%d owner=%d slot=%d card=%d packed=%u cost=%d elixir=%d queue_before=%d queue_after=%d json=%s\n",
           tick, owner, slot, card_id, selection.packed, cost, elixir, before, after, text);
    fflush(stdout);
    release_json_object(json);
    return after == before + 1 ? 1 : -1;
}

static uint64_t snapshot_digest(const unsigned char *payload, int32_t size) {
    const uint64_t offset = UINT64_C(1469598103934665603);
    const uint64_t prime = UINT64_C(1099511628211);
    uint64_t value = offset;
    for (int32_t index = 0; index < size; ++index) {
        value ^= payload[index];
        value *= prime;
    }
    return value;
}

static int create_snapshot(void *manager, void **wrapper_out, uint64_t *digest_out, int32_t *size_out) {
    void *wrapper = NULL;
    if (!manager || !wrapper_out || !digest_out || !size_out) return 0;
    void *service = ((void *(*)(void))(base + 0xc8c5b8))();
    if (!getenv("NASHURA_QUIET_SNAPSHOTS")) {
        printf("HEADLESS_SNAPSHOT_CONTEXT service=%p adapter=%d\n", service,
               getenv("NASHURA_SNAPSHOT_ADAPTER") != NULL);
        fflush(stdout);
    }
    if (getenv("NASHURA_SNAPSHOT_ADAPTER")) {
        /* Exact ad9a50 stream prologue and original serializers. The absent
         * client controller is represented by NULL, never by a fake object.
         * Android byte parity covers the paired fixture's serialized state;
         * this does not establish original client-wrapper equivalence. */
        void *context = service ? (void *)pointer_at((uintptr_t)service + 0x48) : NULL;
        wrapper = ((game_alloc_fn)(base + 0x17e4b20))(0x90);
        if (!wrapper) return 0;
        ((void (*)(void *))(base + 0x105e870))(wrapper);
        void *stream = ((void *(*)(void *))(base + 0x121bd78))(wrapper);
        uintptr_t vtable = pointer_at((uintptr_t)stream);
        void (*write_byte)(void *, int) = (void (*)(void *, int))pointer_at(vtable + 0x78);
        void (*write_bool)(void *, int) = (void (*)(void *, int))pointer_at(vtable + 0x40);
        write_byte(stream, 0);
        write_byte(stream, pointer_at((uintptr_t)manager + 0x218) != 0);
        ((void (*)(void *))(base + 0x12141bc))(stream);
        write_bool(stream, 1);
        ((void (*)(void *, void *, int, void *, int))(base + 0x10632b0))(
            manager, stream, 1, context, 0);
        ((void (*)(void *, void *, int, void *, int, int, int))(base + 0x1062c3c))(
            manager, stream, 1, context, 0, 0, 0);
    } else {
        cr_call_state_snapshot(manager, &wrapper, (void *)(base + 0xad9a50));
    }
    if (!wrapper) return 0;
    int32_t size = -1;
    const unsigned char *payload = NULL;
    memcpy(&size, (unsigned char *)wrapper + 0x1c, sizeof(size));
    memcpy(&payload, (unsigned char *)wrapper + 0x40, sizeof(payload));
    if (size < 0 || size > 32 * 1024 * 1024 || (size && !payload)) {
        ((state_snapshot_delete_fn)(base + 0x105e8bc))(wrapper);
        return 0;
    }
    *wrapper_out = wrapper;
    *size_out = size;
    *digest_out = snapshot_digest(payload, size);
    if (!getenv("NASHURA_QUIET_SNAPSHOTS")) {
        printf("HEADLESS_GAME_STATE_SNAPSHOT_CREATE wrapper=%p bytes=%d digest=%016llx\n",
               wrapper, size, (unsigned long long)*digest_out);
        fflush(stdout);
    }
    return 1;
}

static int write_all(int fd, const void *source, size_t size) {
    const unsigned char *bytes = source;
    while (size) {
        ssize_t n = write(fd, bytes, size);
        if (n < 0 && errno == EINTR) continue;
        if (n <= 0) return 0;
        bytes += n;
        size -= (size_t)n;
    }
    return 1;
}

/* Private archive format: little-endian int32 tick, int32 byte count, then the
 * original serializer payload. O_EXCL prevents overwriting prior evidence. */
static int trace_snapshot(int fd, void *state, int32_t tick) {
    void *wrapper = NULL;
    uint64_t digest;
    int32_t size;
    if (!create_snapshot(state, &wrapper, &digest, &size)) return 0;
    uintptr_t payload = pointer_at((uintptr_t)wrapper + 0x40);
    int32_t header[2] = {tick, size};
    int ok = write_all(fd, header, sizeof(header)) && write_all(fd, (void *)payload, (size_t)size);
    ((state_snapshot_delete_fn)(base + 0x105e8bc))(wrapper);
    return ok;
}

static int consume_snapshot_prelude(void *stream) {
    uintptr_t vtable = 0;
    memcpy(&vtable, stream, sizeof(vtable));
    if (!vtable) return 0;
    uintptr_t read_byte_address = 0, read_bool_address = 0;
    memcpy(&read_byte_address, (unsigned char *)vtable + 0x150, sizeof(read_byte_address));
    memcpy(&read_bool_address, (unsigned char *)vtable + 0xe0, sizeof(read_bool_address));
    if (!read_byte_address || !read_bool_address) return 0;
    int32_t compression = ((byte_stream_read_fn)read_byte_address)(stream) & 0xff;
    int32_t version = ((byte_stream_read_fn)read_byte_address)(stream) & 0xff;
    int32_t full_state = ((byte_stream_read_fn)read_bool_address)(stream) & 1;
    return compression == 0 && version == 0 && full_state;
}

static int restore_snapshot(void *manager, void *wrapper, int32_t expected_tick,
                            uint64_t expected_digest, int32_t expected_size) {
    if (!manager || !wrapper) return 0;
    void *stream = (unsigned char *)wrapper + 0x08;
    ((byte_stream_reset_fn)(base + 0x121220c))(stream);
    if (!consume_snapshot_prelude(stream)) return 0;
    ((game_state_deserialize_header_fn)(base + 0x10634c0))(manager, stream);
    ((game_state_deserialize_body_fn)(base + 0x106379c))(manager, stream, 1, NULL);
    void *verification = NULL;
    uint64_t digest = 0;
    int32_t size = -1;
    int verified = create_snapshot(manager, &verification, &digest, &size);
    if (verification) ((state_snapshot_delete_fn)(base + 0x105e8bc))(verification);
    int32_t tick = ((game_tick_fn)(base + 0x1062bc0))(manager);
    printf("HEADLESS_GAME_STATE_SNAPSHOT_RESTORE tick=%d expected_tick=%d bytes=%d expected_bytes=%d digest=%016llx expected_digest=%016llx verified=%d\n",
           tick, expected_tick, size, expected_size, (unsigned long long)digest,
           (unsigned long long)expected_digest,
           verified && tick == expected_tick && size == expected_size && digest == expected_digest);
    fflush(stdout);
    return verified && tick == expected_tick && size == expected_size && digest == expected_digest;
}

static int extract_location_id(const char *text, int32_t *location_out) {
    const char *value;
    char *end;
    long parsed;
    if (!text || !location_out) return 0;
    value = strstr(text, "\"location\":");
    if (!value) return 0;
    value += strlen("\"location\":");
    errno = 0;
    parsed = strtol(value, &end, 10);
    if (errno || end == value || parsed <= 0 || parsed > INT32_MAX) return 0;
    *location_out = (int32_t)parsed;
    return 1;
}

static int inspect_content_context(uintptr_t expected_context, uintptr_t *root_out,
                                   uintptr_t *context_out, unsigned char *ready_out) {
    uintptr_t root = pointer_at(base + 0x19ad878);
    uintptr_t context = root ? pointer_at(root + 0x20) : 0;
    unsigned char ready = context ? *(const unsigned char *)(context + 0x6e8) : 0;
    if (root_out) *root_out = root;
    if (context_out) *context_out = context;
    if (ready_out) *ready_out = ready;
    printf("HEADLESS_GAME_STATE_CONTENT root=%p current_context=%p expected_context=%p ready=%u matches=%d\n",
           (void *)root, (void *)context, (void *)expected_context, ready,
           context != 0 && context == expected_context && ready == 1);
    fflush(stdout);
    return context != 0 && context == expected_context && ready == 1;
}

static void describe_location_lookup(uintptr_t context, int32_t location_id) {
    uintptr_t auxiliary = context ? pointer_at(context + 0xad8) : 0;
    uintptr_t auxiliary_vtable = auxiliary ? pointer_at(auxiliary) : 0;
    int32_t type_index = ((logic_data_type_index_fn)(base + 0xf74718))(location_id);
    uintptr_t table = (context && type_index > 0 && type_index < 219)
        ? pointer_at(context + (uintptr_t)type_index * 8) : 0;
    int32_t kind = -1, count = -1, capacity = -1;
    uintptr_t rows = 0;
    if (table) {
        memcpy(&kind, (const void *)(table + 0x84), sizeof(kind));
        memcpy(&capacity, (const void *)(table + 0x38), sizeof(capacity));
        memcpy(&count, (const void *)(table + 0x3c), sizeof(count));
        rows = pointer_at(table + 0x30);
    }
    printf("HEADLESS_GAME_STATE_LOOKUP_DIAGNOSTIC id=%d type_index=%d context_aux=%p context_aux_vtable=%p table=%p kind=%d count=%d capacity=%d rows=%p\n",
           location_id, type_index, (void *)auxiliary, (void *)auxiliary_vtable,
           (void *)table, kind, count, capacity, (void *)rows);
    if (rows && count > 0 && count <= 1000000) {
        int samples = count < 5 ? count : 5;
        for (int index = 0; index < samples; ++index) {
            uintptr_t row = pointer_at(rows + (uintptr_t)index * 8);
            uint32_t id40 = row ? *(const uint32_t *)(row + 0x40) : 0;
            uint32_t id10 = row ? *(const uint32_t *)(row + 0x10) : 0;
            printf("HEADLESS_GAME_STATE_LOOKUP_ROW index=%d row=%p id40=%u id10=%u\n",
                   index, (void *)row, id40, id10);
        }
    }
    fflush(stdout);
}

static int prewarm_location(uintptr_t context, int32_t location_id, int allow_ensure,
                            void **location_out, void **tilemap_before_out, void **tilemap_after_out) {
    void *location = NULL, *tilemap_before = NULL, *tilemap_after = NULL;
    if (context && location_id > 0) {
        location = ((logic_data_by_id_fn)(base + 0xdd1340))((void *)context, location_id);
        if (location) {
            tilemap_before = ((tilemap_lookup_fn)(base + 0xdf8508))(location);
            tilemap_after = tilemap_before;
            if (!tilemap_before && allow_ensure) {
                printf("HEADLESS_GAME_STATE_TILEMAP_ENSURE_ENTER location=%p id=%d\n", location, location_id);
                fflush(stdout);
                ((tilemap_ensure_fn)(base + 0x857608))(location);
                tilemap_after = ((tilemap_lookup_fn)(base + 0xdf8508))(location);
            }
        }
    }
    if (location_out) *location_out = location;
    if (tilemap_before_out) *tilemap_before_out = tilemap_before;
    if (tilemap_after_out) *tilemap_after_out = tilemap_after;
    printf("HEADLESS_GAME_STATE_PREWARM context=%p id=%d location=%p tilemap_before=%p tilemap_after=%p allow_ensure=%d ready=%d\n",
           (void *)context, location_id, location, tilemap_before, tilemap_after,
           allow_ensure, tilemap_after != NULL);
    fflush(stdout);
    return tilemap_after != NULL;
}

static int run_game_state_battle(int step_once, int allow_commands, int snapshot_mode,
                                int benchmark_match_index) {
    void *root = NULL, *battle = NULL, *commands = NULL, *injected_commands = NULL, *events = NULL;
    void *context_a, *context_b, *world, *queue;
    void *location_data = NULL, *tilemap_before = NULL, *tilemap_after = NULL;
    uintptr_t content_root = 0, content_context = 0;
    unsigned char content_ready = 0;
    int content_context_ok, context_b_ok, prewarm_ok = 0;
    int32_t location_id, command_count = -1, loaded_command_count = -1, event_count = -1;
    int32_t tick_before, tick_after, snapshot_tick = -1;
    void *snapshot = NULL;
    uint64_t snapshot_hash = 0;
    int32_t snapshot_bytes = -1;
    struct phase_metrics phase = {0};
    phase.enabled = phase_timing_requested();
    const int minimal_benchmark = minimal_benchmark_requested();
    const char *resource_text = getenv("NASHURA_THREAD_RESOURCES");
    const int thread_resources = resource_text && !strcmp(resource_text, "1");
    if (thread_resources && !minimal_benchmark) return -57;
    phase.first_queue_change_tick = -1;
    phase.last_queue_change_tick = -1;
    const uint64_t canary = UINT64_C(0x93e726ac415bd80f);
    unsigned char *state;
    if (!battle_json || battle_json_size < 2 || !extract_location_id((char *)battle_json, &location_id)) return -30;
    uint64_t json_parse_start = phase.enabled ? monotonic_ns() : 0;
    root = parse_json_text((char *)battle_json);
    if (phase.enabled) phase.battle_json_parse_ns = monotonic_ns() - json_parse_start;
    if (!root) return -31;
    battle = lookup_json_member(root, "battle");
    commands = lookup_json_member(root, "cmd");
    injected_commands = lookup_json_member(root, "inject");
    events = lookup_json_member(root, "evt");
    command_count = 0;
    if (!battle || !inspect_json_array(commands, &loaded_command_count) ||
            (injected_commands && !inspect_json_array(injected_commands, &command_count)) ||
            (!allow_commands && (loaded_command_count != 0 || command_count != 0))
            || !inspect_json_array(events, &event_count) || event_count != 0) {
        release_json_object(root);
        return -32;
    }
    phase.loaded_command_count = loaded_command_count;
    phase.injected_command_count = command_count;
    printf("HEADLESS_GAME_STATE_JSON_VALID battle=1 loaded_cmd=%d inject_cmd=%d evt=%d allow_commands=%d snapshot_mode=%d\n",
           loaded_command_count, command_count, event_count, allow_commands, snapshot_mode);
    fflush(stdout);
    context_a = ((void *(*)(void))(base + 0x80031c))();
    context_b = ((void *(*)(void))(base + 0xeaf1c8))();
    if (!inspect_content_identity("battle-before")) {
        release_json_object(root);
        return -54;
    }
    content_context_ok = inspect_content_context((uintptr_t)context_a, &content_root,
                                                 &content_context, &content_ready);
    context_b_ok = (uintptr_t)context_b == base + 0x18eedb0;
    describe_location_lookup((uintptr_t)context_a, location_id);
    if (content_context_ok) {
        prewarm_ok = prewarm_location((uintptr_t)context_a, location_id, content_context_ok,
                                      &location_data, &tilemap_before, &tilemap_after);
    }
    printf("HEADLESS_GAME_STATE_PRECONDITIONS context_a=%p context_b=%p context_b_expected=%p content_root=%p content_context=%p ready=%u context_a_ok=%d context_b_ok=%d location=%p tilemap_before=%p tilemap_after=%p prewarm_ok=%d\n",
           context_a, context_b, (void *)(base + 0x18eedb0), (void *)content_root,
           (void *)content_context, content_ready, content_context_ok, context_b_ok,
           location_data, tilemap_before, tilemap_after, prewarm_ok);
    fflush(stdout);
    if (!content_context_ok || !context_b_ok || !prewarm_ok) {
        release_json_object(root);
        return -33;
    }
    state = ((game_alloc_fn)(base + 0x17e4b20))(0x240);
    if (!state) {
        release_json_object(root);
        return -34;
    }
    memcpy(state + 0x230, &canary, sizeof(canary));
    memcpy(state + 0x238, &canary, sizeof(canary));
    puts("HEADLESS_GAME_STATE_CONSTRUCTOR_ENTER size=0x230 mode=1");
    fflush(stdout);
    ((game_constructor_fn)(base + 0x1060ba8))(state, 1, context_a, context_b, 0);
    ((game_setup_fn)(base + 0x1060f84))(state, 1);
    puts("HEADLESS_GAME_STATE_SETUP_RETURN");
    fflush(stdout);
    puts("HEADLESS_GAME_STATE_LOAD_ENTER");
    fflush(stdout);
    uint64_t game_load_start = phase.enabled ? monotonic_ns() : 0;
    ((game_load_fn)(base + 0x1061b1c))(state, root, NULL, NULL);
    if (phase.enabled) phase.game_state_load_ns = monotonic_ns() - game_load_start;
    tick_before = ((game_tick_fn)(base + 0x1062bc0))(state);
    memcpy(&world, state + 0xa8, sizeof(world));
    memcpy(&queue, state + 0x38, sizeof(queue));
    printf("HEADLESS_GAME_STATE_LOAD_RETURN tick=%d world_nonnull=%d queue_nonnull=%d\n",
           tick_before, world != NULL, queue != NULL);
    fflush(stdout);
    if (!world || !queue) {
        ((game_destroy_fn)(base + 0x10610b0))(state);
        ((game_destroy_fn)(base + 0x988844))(state);
        ((game_delete_fn)(base + 0x17e4b50))(state);
        release_json_object(root);
        return -35;
    }
    if (allow_commands && command_count > 0) {
        int command_status = parse_and_append_commands(state, injected_commands, queue, &phase);
        printf("HEADLESS_GAME_STATE_COMMAND_STATUS=%d queue_count_after=%d\n",
               command_status, queue_count(queue));
        fflush(stdout);
        if (command_status != 1) {
            ((game_destroy_fn)(base + 0x10610b0))(state);
            ((game_destroy_fn)(base + 0x988844))(state);
            ((game_delete_fn)(base + 0x17e4b50))(state);
            release_json_object(root);
            return -37;
        }
    }
    const char *snapshot_at_text = getenv("NASHURA_SNAPSHOT_AT");
    int snapshot_at = snapshot_at_text ? atoi(snapshot_at_text) : 0;
    if (snapshot_mode && snapshot_at == 0) {
        if (!create_snapshot(state, &snapshot, &snapshot_hash, &snapshot_bytes)) {
            ((game_destroy_fn)(base + 0x10610b0))(state);
            ((game_destroy_fn)(base + 0x988844))(state);
            ((game_delete_fn)(base + 0x17e4b50))(state);
            release_json_object(root);
            return -38;
        }
        ((byte_stream_reset_fn)(base + 0x0121221c))((unsigned char *)snapshot + 0x08);
        snapshot_tick = tick_before;
    }
    if (step_once) {
        puts("HEADLESS_GAME_STATE_STEP_ENTER");
        fflush(stdout);
        const char *steps_text = getenv("NASHURA_BATTLE_STEPS");
        int steps = steps_text ? atoi(steps_text) : 1;
        if (steps < 1 || steps > 10000) return -45;
        tick_after = tick_before;
        int finalized = observe_battle(state, "before");
        if (finalized < 0) return -46;
        if (phase.enabled) phase.queue_at_step_start = queue_count(queue);
        int completed = 0;
        int sample_every = 1;
        int sample_phase = 0;
        int sample_phase_period = 1;
        int timed_samples = 0;
        if (minimal_benchmark) {
            const char *sample_text = getenv("NASHURA_STEP_SAMPLE_EVERY");
            sample_every = sample_text ? atoi(sample_text) : 16;
            if (sample_every < 1 || sample_every > 10000) return -53;
            sample_phase_period = sample_every < steps ? sample_every : steps;
            sample_phase = benchmark_match_index % sample_phase_period;
        }
        const char *trace_file = getenv("NASHURA_TRACE_SNAPSHOTS");
        int trace_fd = trace_file ? open(trace_file, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC, 0600) : -1;
        if (trace_file && (trace_fd < 0 || !trace_snapshot(trace_fd, state, tick_before))) return -52;
        if (native_phases_installed) native_phase_reset();
        struct thread_resources resources_before = {0};
        if (thread_resources) resources_before = read_thread_resources();
        uint64_t elapsed_start = monotonic_ns(), total_step_ns = 0, worst_step_ns = 0;
        uint64_t min_step_ns = UINT64_MAX;
        uint64_t durations[10000];
        if (minimal_benchmark) {
            /* Rotate tick phase across serial repeats to avoid periodic aliasing. */
            int sample_countdown = sample_phase;
            for (; completed < steps && !finalized; ++completed) {
                int measure_step = sample_countdown == 0;
                uint64_t step_start = measure_step ? monotonic_ns() : 0;
                ((game_step_fn)(base + 0x10620a4))(state);
                if (measure_step) {
                    uint64_t step_ns = monotonic_ns() - step_start;
                    durations[timed_samples++] = step_ns;
                    total_step_ns += step_ns;
                    if (step_ns > worst_step_ns) worst_step_ns = step_ns;
                    if (step_ns < min_step_ns) min_step_ns = step_ns;
                    sample_countdown = sample_every - 1;
                } else {
                    --sample_countdown;
                }
                uintptr_t current_world = pointer_at((uintptr_t)state + 0xa8);
                finalized = current_world ? *(unsigned char *)(current_world + 0x1e0) : -1;
                tick_after = tick_before + completed + 1;
                if (finalized < 0 || finalized > 1) return -46;
            }
        } else {
            for (; completed < steps && !finalized; ++completed) {
                int previous = tick_after;
                if (getenv("NASHURA_DEPLOY_HAND") && previous % 100 == 0) {
                    for (int owner = 0; owner < 2; ++owner)
                        if (queue_hand_deploy(state, owner) < 0) return -48;
                }
                int32_t queue_before = phase.enabled ? queue_count(queue) : -1;
                uint64_t step_start = monotonic_ns();
                ((game_step_fn)(base + 0x10620a4))(state);
                uint64_t step_ns = monotonic_ns() - step_start;
                durations[timed_samples++] = step_ns;
                total_step_ns += step_ns;
                if (step_ns > worst_step_ns) worst_step_ns = step_ns;
                if (step_ns < min_step_ns) min_step_ns = step_ns;
                int32_t queue_after = phase.enabled ? queue_count(queue) : -1;
                if (phase.enabled && queue_before != queue_after) {
                    if (queue_after < queue_before) phase.queue_removed += queue_before - queue_after;
                    else phase.queue_added += queue_after - queue_before;
                    ++phase.queue_change_steps;
                    if (phase.first_queue_change_tick < 0) phase.first_queue_change_tick = previous + 1;
                    phase.last_queue_change_tick = previous + 1;
                }
                tick_after = ((game_tick_fn)(base + 0x1062bc0))(state);
                if (tick_after != previous + 1) {
                    printf("HEADLESS_BATTLE_STEP_STALLED previous=%d current=%d\n", previous, tick_after);
                    fflush(stdout);
                    return -47;
                }
                uintptr_t current_world = pointer_at((uintptr_t)state + 0xa8);
                finalized = current_world ? *(unsigned char *)(current_world + 0x1e0) : -1;
                if (tick_after <= 30 || tick_after % 100 == 0 || finalized)
                    finalized = observe_battle(state, "step");
                if (finalized < 0 || finalized > 1) return -46;
                if (trace_fd >= 0 && !trace_snapshot(trace_fd, state, tick_after)) return -52;
                if (snapshot_mode && !snapshot && tick_after == snapshot_at) {
                    if (!create_snapshot(state, &snapshot, &snapshot_hash, &snapshot_bytes)) return -38;
                    ((byte_stream_reset_fn)(base + 0x121221c))((unsigned char *)snapshot + 0x08);
                    snapshot_tick = tick_after;
                }
            }
        }
        uint64_t elapsed_ns = monotonic_ns() - elapsed_start;
        if (thread_resources && !report_thread_resources(resources_before, read_thread_resources(),
                elapsed_start, elapsed_start + elapsed_ns)) return -57;
        native_phase_report();
        if (minimal_benchmark) {
            if (!timed_samples) {
                if (trace_fd >= 0) close(trace_fd);
                if (snapshot) ((state_snapshot_delete_fn)(base + 0x105e8bc))(snapshot);
                ((game_destroy_fn)(base + 0x10610b0))(state);
                ((game_destroy_fn)(base + 0x988844))(state);
                ((game_delete_fn)(base + 0x17e4b50))(state);
                release_json_object(root);
                return -55;
            }
            tick_after = ((game_tick_fn)(base + 0x1062bc0))(state);
            uintptr_t final_world = pointer_at((uintptr_t)state + 0xa8);
            finalized = final_world ? *(unsigned char *)(final_world + 0x1e0) : -1;
            if (tick_after != tick_before + completed || finalized < 0 || finalized > 1) return -47;
        }
        if (phase.enabled) phase.queue_at_end = queue_count(queue);
        if (trace_fd >= 0) close(trace_fd);
        qsort(durations, (size_t)timed_samples, sizeof(durations[0]), compare_duration);
        uint64_t p50 = timed_samples ? durations[(timed_samples - 1) * 50 / 100] : 0;
        uint64_t p95 = timed_samples ? durations[(timed_samples - 1) * 95 / 100] : 0;
        uint64_t p99 = timed_samples ? durations[(timed_samples - 1) * 99 / 100] : 0;
        struct rusage usage;
        memset(&usage, 0, sizeof(usage));
        getrusage(RUSAGE_SELF, &usage);
        printf("HEADLESS_BATTLE_METRICS completed=%d wall_ns=%llu step_total_ns=%llu step_min_ns=%llu step_max_ns=%llu p50_ns=%llu p95_ns=%llu p99_ns=%llu sample_count=%d sample_every=%d sample_phase=%d sample_phase_period=%d loaded_commands=%d injected_commands=%d peak_rss_kb=%ld trace=%d start_ns=%llu end_ns=%llu\n",
               completed, (unsigned long long)elapsed_ns, (unsigned long long)total_step_ns,
               (unsigned long long)min_step_ns, (unsigned long long)worst_step_ns,
               (unsigned long long)p50, (unsigned long long)p95, (unsigned long long)p99,
               timed_samples, sample_every, sample_phase, sample_phase_period,
               loaded_command_count, command_count,
               usage.ru_maxrss, trace_file != NULL, (unsigned long long)elapsed_start,
               (unsigned long long)(elapsed_start + elapsed_ns));
        if (getenv("NASHURA_MEMORY_SAMPLE")) {
            FILE *memory = fopen("/proc/self/smaps_rollup", "r");
            long rss = -1, pss = -1;
            char line[256];
            if (memory) {
                while (fgets(line, sizeof(line), memory)) {
                    if (!strncmp(line, "Rss:", 4)) rss = strtol(line + 4, NULL, 10);
                    if (!strncmp(line, "Pss:", 4)) pss = strtol(line + 4, NULL, 10);
                }
                fclose(memory);
            }
            printf("HEADLESS_BATTLE_MEMORY rss_kb=%ld pss_kb=%ld\n", rss, pss);
        }
        fflush(stdout);
        observe_battle(state, "after");
        printf("HEADLESS_GAME_STATE_STEP_RETURN tick_before=%d tick_after=%d completed=%d requested=%d finalized=%d\n",
               tick_before, tick_after, completed, steps, finalized);
        if (phase.enabled) {
            printf("HEADLESS_PHASE_TIMING scope=diagnostic-only battle_json_parse_ns=%llu game_state_load_ns=%llu injected_logic_parse_ns=%llu injected_logic_append_ns=%llu loaded_commands=%d injected_commands=%d queue_at_step_start=%d queue_at_end=%d queue_removed=%d queue_added=%d queue_change_steps=%d first_queue_change_tick=%d last_queue_change_tick=%d\n",
                   (unsigned long long)phase.battle_json_parse_ns,
                   (unsigned long long)phase.game_state_load_ns,
                   (unsigned long long)phase.logic_command_parse_ns,
                   (unsigned long long)phase.logic_command_append_ns,
                   phase.loaded_command_count, phase.injected_command_count,
                   phase.queue_at_step_start, phase.queue_at_end,
                   phase.queue_removed, phase.queue_added, phase.queue_change_steps,
                   phase.first_queue_change_tick, phase.last_queue_change_tick);
        }
        fflush(stdout);
    } else {
        tick_after = tick_before;
    }
    if (memcmp(state + 0x230, &canary, sizeof(canary)) || memcmp(state + 0x238, &canary, sizeof(canary))) {
        if (snapshot) ((state_snapshot_delete_fn)(base + 0x105e8bc))(snapshot);
        ((game_destroy_fn)(base + 0x10610b0))(state);
        ((game_destroy_fn)(base + 0x988844))(state);
        ((game_delete_fn)(base + 0x17e4b50))(state);
        release_json_object(root);
        return -36;
    }
    int snapshot_status = 1;
    if (snapshot_mode) {
        void *expected_final = NULL;
        uint64_t expected_digest = 0;
        int32_t expected_size = -1;
        if (!snapshot || !create_snapshot(state, &expected_final, &expected_digest, &expected_size)) return -38;
        ((state_snapshot_delete_fn)(base + 0x105e8bc))(expected_final);
        snapshot_status = restore_snapshot(state, snapshot, snapshot_tick, snapshot_hash, snapshot_bytes);
        ((state_snapshot_delete_fn)(base + 0x105e8bc))(snapshot);
        snapshot = NULL;
        if (!snapshot_status) {
            ((game_destroy_fn)(base + 0x10610b0))(state);
            ((game_destroy_fn)(base + 0x988844))(state);
            ((game_delete_fn)(base + 0x17e4b50))(state);
            release_json_object(root);
            return -39;
        }
        for (int32_t tick = snapshot_tick; tick < tick_after; ++tick) {
            ((game_step_fn)(base + 0x10620a4))(state);
            if (((game_tick_fn)(base + 0x1062bc0))(state) != tick + 1) return -47;
        }
        void *actual_final = NULL;
        uint64_t actual_digest = 0;
        int32_t actual_size = -1;
        int captured = create_snapshot(state, &actual_final, &actual_digest, &actual_size);
        if (actual_final) ((state_snapshot_delete_fn)(base + 0x105e8bc))(actual_final);
        int equal = captured && expected_size == actual_size && expected_digest == actual_digest;
        printf("HEADLESS_SNAPSHOT_CONTINUATION from=%d to=%d expected_bytes=%d actual_bytes=%d expected_digest=%016llx actual_digest=%016llx equal=%d\n",
               snapshot_tick, tick_after, expected_size, actual_size,
               (unsigned long long)expected_digest, (unsigned long long)actual_digest, equal);
        fflush(stdout);
        if (!equal) return -49;
        last_battle_digest = actual_digest;
        last_battle_bytes = actual_size;
    }
    if (!snapshot_mode && getenv("NASHURA_BENCHMARK_VALIDATE")) {
        void *verification = NULL;
        if (!create_snapshot(state, &verification, &last_battle_digest, &last_battle_bytes)) return -38;
        ((state_snapshot_delete_fn)(base + 0x105e8bc))(verification);
    }
    ((game_destroy_fn)(base + 0x10610b0))(state);
    ((game_destroy_fn)(base + 0x988844))(state);
    ((game_delete_fn)(base + 0x17e4b50))(state);
    release_json_object(root);
    printf("HEADLESS_GAME_STATE_RELEASE_RETURN step_once=%d tick=%d command_count=%d snapshot_status=%d\n",
           step_once, tick_after, command_count, snapshot_status);
    fflush(stdout);
    return inspect_content_identity("battle-after") ? 1 : -54;
}

static int inspect_tables(uintptr_t context) {
    unsigned int populated = 0, total = 0;
    for (unsigned int slot = 0; slot < 219; ++slot) {
        uintptr_t object = pointer_at(context + slot * 8);
        if (!object) continue;
        int capacity, count, kind;
        memcpy(&capacity, (const void *)(object + 0x38), 4);
        memcpy(&count, (const void *)(object + 0x3c), 4);
        memcpy(&kind, (const void *)(object + 0x84), 4);
        if (kind != (int)slot || pointer_at(object + 0x78) != context
                || count < 0 || count > capacity || capacity > 1000000) return -16;
        unsigned int nonnull = 0;
        uintptr_t rows = pointer_at(object + 0x30);
        for (int row = 0; row < count; ++row) nonnull += pointer_at(rows + row * 8) != 0;
        printf("HEADLESS_CATALOG_TABLE=%u count=%d nonnull=%u\n", slot, count, nonnull);
        fflush(stdout);
        if (nonnull != (unsigned int)count) return -17;
        populated++;
        total += count;
    }
    printf("HEADLESS_CATALOG_TABLE_SUMMARY=%u rows=%u\n", populated, total);
    fflush(stdout);
    return 1;
}

static int construct_game_state(void) {
    uintptr_t root = pointer_at(base + 0x19ad878);
    uintptr_t context = root ? pointer_at(root + 0x20) : 0;
    if (!context || *(const unsigned char *)(context + 0x6e8) != 1) return -18;
    uintptr_t a = ((uintptr_t (*)(void))(base + 0x80031c))();
    uintptr_t b = ((uintptr_t (*)(void))(base + 0xeaf1c8))();
    if (a != context || b != base + 0x18eedb0) return -19;
    unsigned char *state = ((void *(*)(size_t))(base + 0x17e4b20))(0x240);
    if (!state) return -20;
    const uint64_t canary = UINT64_C(0x93e726ac415bd80f);
    memcpy(state + 0x230, &canary, 8);
    memcpy(state + 0x238, &canary, 8);
    puts("HEADLESS_GAME_STATE_CONSTRUCTOR_ENTER size=0x230 mode=1 context=original");
    fflush(stdout);
    ((void (*)(void *, int, uintptr_t, uintptr_t, int))(base + 0x1060ba8))(state, 1, a, b, 0);
    puts("HEADLESS_GAME_STATE_CONSTRUCTOR_RETURN");
    fflush(stdout);
    ((void (*)(void *, int))(base + 0x1060f84))(state, 1);
    puts("HEADLESS_GAME_STATE_SETUP_RETURN");
    fflush(stdout);
    int valid = !memcmp(state + 0x230, &canary, 8) && !memcmp(state + 0x238, &canary, 8)
        && pointer_at((uintptr_t)state + 0x208) == a && pointer_at((uintptr_t)state + 0x210) == b;
    printf("HEADLESS_GAME_STATE_FIELDS_AND_CANARY=%d\n", valid);
    ((void (*)(void *))(base + 0x10610b0))(state);
    ((void (*)(void *))(base + 0x988844))(state);
    ((void (*)(void *))(base + 0x17e4b50))(state);
    puts("HEADLESS_GAME_STATE_DESTROY_RETURN");
    fflush(stdout);
    return valid ? 1 : -21;
}

static int load_catalog(void) {
    uintptr_t root = pointer_at(base + 0x19ad878);
    uintptr_t context = root ? pointer_at(root + 0x20) : 0;
    if (!context) return -8;
    if (!inspect_content_identity("catalog-before")) return -54;
    // Original initialize 0x756c94..0x756d10 sets up the packed reader and
    // registers this concrete factory. Native ownership lasts to process exit.
    if (pointer_at(base + 0x19f1228)) return -9;
    ((void (*)(int))(base + 0x119597c))(1);
    void *factory = ((void *(*)(size_t))(base + 0x17e4b20))(8);
    if (!factory) return -10;
    ((void (*)(void *))(base + 0x1194a40))(factory);
    if (pointer_at((uintptr_t)factory) != base + 0x18ba4f8) return -11;
    const unsigned char flags[2] = {1, 0};
    ((void (*)(void *, const char *, const void *))(base + 0x11919d4))(factory, "", flags);
    puts("HEADLESS_CATALOG_FACTORY_REGISTERED");
    fflush(stdout);
    uintptr_t reader = base + 0x19c3c10;
    if (pointer_at(reader) != base + 0x1887140) return -12;
    uintptr_t list = ((uintptr_t (*)(void))(base + 0xe16fe4))();
    if (!list) return -13;
    int count;
    memcpy(&count, (const void *)(list + 12), sizeof(count));
    printf("HEADLESS_CATALOG_LIST_COUNT=%d\n", count);
    fflush(stdout);
    if (count < 1 || count > 4096) return -14;
    unsigned char listener[0x28] __attribute__((aligned(16)));
    ((void (*)(void *))(base + 0x1252638))(listener);
    puts("HEADLESS_CATALOG_PRELOAD_ENTER");
    fflush(stdout);
    ((void (*)(void *, uintptr_t, void *))(base + 0xe16a08))((void *)reader, list, listener);
    int state;
    memcpy(&state, listener + 0x20, sizeof(state));
    printf("HEADLESS_CATALOG_PRELOAD_STATE=%d\n", state);
    fflush(stdout);
    ((void (*)(void *))(base + 0x12526c0))(listener);
    if (state != 3) return -15;
    unsigned char loader[0xb8] __attribute__((aligned(16)));
    ((void (*)(void *, uintptr_t, uintptr_t))(base + 0xc828d8))(loader, context, list);
    puts("HEADLESS_CATALOG_LOADER_START_ENTER");
    fflush(stdout);
    ((void (*)(void *))(base + 0xc833ec))(loader);
    for (int attempt = 0; attempt < 4096; ++attempt) {
        unsigned char ready = *(const unsigned char *)(context + 0x6e8);
        if (ready) break;
        memcpy(&state, loader + 0x10, sizeof(state));
        printf("HEADLESS_CATALOG_STEP_ENTER=%d state=%d\n", attempt, state);
        fflush(stdout);
        int result = ((int (*)(void *))(base + 0xc8346c))(loader);
        memcpy(&state, loader + 0x10, sizeof(state));
        printf("HEADLESS_CATALOG_STEP_RETURN=%d state=%d result=%d ready=%u\n",
               attempt, state, result, *(const unsigned char *)(context + 0x6e8));
        fflush(stdout);
        if (result != 1) break;
    }
    printf("HEADLESS_CATALOG_READY_OBSERVED=%u\n", *(const unsigned char *)(context + 0x6e8));
    fflush(stdout);
    int inspected = inspect_tables(context);
    ((void (*)(void *))(base + 0xc8eb64))(loader);
    if (!inspect_content_identity("catalog-after")) return -54;
    // Completing the task reports control flow only; the Java launcher never
    // interprets this as a catalog or battle readiness verdict.
    return inspected;
}

static void destroy_task(struct task *task) {
    ((void (*)(void *))(base + 0x17e4b50))(task);
    atomic_store_explicit(&destroyed, 1, memory_order_release);
}

static void run_task(struct task *task) {
    (void)task;
    char name[16] = {0};
    prctl(PR_GET_NAME, name);
    printf("HEADLESS_TASK_THREAD=%s\n", name);
    printf("HEADLESS_TASK_TID=%ld\n", syscall(SYS_gettid));
    fflush(stdout);
    if (strcmp(name, "Mainloop")) { task_status = -2; return; }
    if (!requested_mode) { task_status = 1; return; }
    int fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
    uintptr_t owner, game, context, previous_context, table, entry;
    int valid = fd >= 0
        && read_pointer(fd, base + 0x1a06ab8, &owner)
        && read_pointer(fd, owner + 8, &game)
        && read_pointer(fd, owner + 0x10, &context)
        && read_pointer(fd, game + 8, &previous_context)
        && context && context == previous_context
        && read_pointer(fd, game, &table) && table == base + 0x17ea568
        && read_pointer(fd, table + 0x18, &entry) && entry == base + 0x756be8;
    if (fd >= 0) close(fd);
    if (!valid) { task_status = -3; return; }
    if (requested_mode == 3) { task_status = load_catalog(); return; }
    if (requested_mode == 4) { task_status = construct_game_state(); return; }
    if (requested_mode >= 5 && requested_mode <= 10) {
        const char *optimization = getenv("NASHURA_NATIVE_OPTIMIZATION");
        const char *native_phases = getenv("NASHURA_NATIVE_PHASES");
        if (native_phases && (!strcmp(native_phases, "1"))) {
            if (optimization || minimal_benchmark_requested() || getenv("NASHURA_RESET_REPEATS")
                    || !install_native_phases(base)) { task_status = -56; return; }
        }
        if (optimization) {
            int installed = !strcmp(optimization, "spatial-y-bound-v1")
                ? install_spatial_y_bound(base) : !strcmp(optimization, "hot-getters-v1")
                ? install_hot_getters(base) : !strcmp(optimization, "component-getters-v1")
                ? install_component_getters(base) : !strcmp(optimization, "saturated-distance-v1")
                ? install_saturated_distance(base) : !strcmp(optimization, "hot-distance-v1")
                ? install_combined_hot_distance(base) : 0;
            if (!installed) { task_status = -55; return; }
        }
        const int step_once = requested_mode == 6 || requested_mode == 8 ||
                              requested_mode == 9 || requested_mode == 10;
        const int allow_commands = requested_mode == 7 || requested_mode == 8 ||
                                   requested_mode == 10;
        const int snapshot_mode = requested_mode == 9 || requested_mode == 10;
        int repeats = getenv("NASHURA_RESET_REPEATS") ? atoi(getenv("NASHURA_RESET_REPEATS")) : 1;
        if (repeats < 1 || repeats > 1000 || (repeats > 1 && !snapshot_mode && !getenv("NASHURA_BENCHMARK_VALIDATE"))) { task_status = -50; return; }
        uint64_t expected_digest = 0;
        int32_t expected_bytes = 0;
        for (int repeat = 0; repeat < repeats; ++repeat) {
            task_status = run_game_state_battle(step_once, allow_commands, snapshot_mode, repeat);
            if (task_status != 1) return;
            int equal = repeat == 0 || (expected_digest == last_battle_digest && expected_bytes == last_battle_bytes);
            printf("HEADLESS_BATTLE_RESET iteration=%d repeats=%d digest=%016llx bytes=%d equal=%d validated=%d\n",
                   repeat + 1, repeats, (unsigned long long)last_battle_digest, last_battle_bytes, equal,
                   snapshot_mode || getenv("NASHURA_BENCHMARK_VALIDATE") != NULL);
            fflush(stdout);
            if (!equal) { task_status = -51; return; }
            expected_digest = last_battle_digest;
            expected_bytes = last_battle_bytes;
        }
        return;
    }
    if (requested_mode == 2) {
        uintptr_t root = 0;
        fd = open("/proc/self/mem", O_RDONLY | O_CLOEXEC);
        valid = fd >= 0 && read_pointer(fd, base + 0x19ad878, &root) && !root;
        if (fd >= 0) close(fd);
        if (!valid) { task_status = -7; return; }
        puts("HEADLESS_CONTENT_BOOTSTRAP_ENTER entry=0x8001a4 root_was_null=1");
        fflush(stdout);
        ((void (*)(void))(base + 0x8001a4))();
        puts("HEADLESS_CONTENT_BOOTSTRAP_RETURN");
        fflush(stdout);
        task_status = inspect_content_identity("bootstrap-after") ? 1 : -54;
        return;
    }
    puts("HEADLESS_REAL_INIT_ENTER context=original_game_field entry=0x756be8");
    fflush(stdout);
    init_result = ((int (*)(uintptr_t, uintptr_t))entry)(game, context);
    printf("HEADLESS_REAL_INIT_RETURN=%d\n", init_result);
    fflush(stdout);
    task_status = 1;
}

static void (*task_vtable[])(struct task *) = {destroy_task, destroy_task, run_task};

JNIEXPORT void JNICALL Java_ArtHeadlessCreateProbe_stopForDebugger(JNIEnv *env, jclass cls) {
    (void)env;
    (void)cls;
    /* Private probe only: allow attachment to this disposable process. */
    if (prctl(PR_SET_DUMPABLE, 1) || prctl(PR_SET_PTRACER, PR_SET_PTRACER_ANY)) {
        puts("HEADLESS_GDB_STOP=PRCTL_FAILED");
        fflush(stdout);
        return;
    }
    printf("HEADLESS_GDB_STOP_PID=%ld\n", (long)getpid());
    fflush(stdout);
    raise(SIGSTOP);
}

static int enqueue_task(int mode) {
    int wait_ms = getenv("NASHURA_TASK_TIMEOUT_MS") ? atoi(getenv("NASHURA_TASK_TIMEOUT_MS")) : 5000;
    if (wait_ms < 1 || wait_ms > 120000) return -53;
    int expected = 0;
    if (!atomic_compare_exchange_strong(&busy, &expected, 1)) return -6;
    base = 0;
    dl_iterate_phdr(find_engine, NULL);
    if (!base) { atomic_store(&busy, 0); return -1; }
    if (!entry_guards()) { atomic_store(&busy, 0); return -22; }
    requested_mode = mode;
    task_status = 0;
    init_result = 0;
    atomic_store_explicit(&destroyed, 0, memory_order_relaxed);
    unsigned char *queue = ((void *(*)(void))(base + 0x130f074))();
    struct task *task = ((void *(*)(size_t))(base + 0x17e4b20))(sizeof(*task));
    task->vtable = task_vtable;
    void *pending = task;
    void (*lock)(void *) = (void (*)(void *))(base + 0x17e4e40);
    void (*unlock)(void *) = (void (*)(void *))(base + 0x17e4e60);
    lock(queue + 0x14);
    uintptr_t end, capacity;
    memcpy(&end, queue + 0x100, sizeof(end));
    memcpy(&capacity, queue + 0x108, sizeof(capacity));
    if (end < capacity) {
        memcpy((void *)end, &pending, sizeof(pending));
        end += sizeof(pending);
        pending = NULL;
    } else {
        end = ((uintptr_t (*)(void *, void **))(base + 0xa63a6c))(queue + 0xf8, &pending);
    }
    memcpy(queue + 0x100, &end, sizeof(end));
    lock(queue + 0x3c);
    int count;
    memcpy(&count, queue + 0x94, sizeof(count));
    count++;
    memcpy(queue + 0x94, &count, sizeof(count));
    unlock(queue + 0x3c);
    ((void (*)(void *))(base + 0x17e50c0))(queue + 0x64);
    unlock(queue + 0x14);
    /* Original vector growth consumes pending; a non-null remainder is an ABI error. */
    if (pending) return -4;
    for (int attempt = 0; attempt < wait_ms; attempt++) {
        if (atomic_load_explicit(&destroyed, memory_order_acquire)) {
            int status = task_status;
            atomic_store(&busy, 0);
            return status;
        }
        usleep(1000);
    }
    /* Keep bridge globals alive until process exit; caller must stop after timeout. */
    return -5;
}

JNIEXPORT jint JNICALL Java_ArtHeadlessCreateProbe_runMainloopTask(JNIEnv *env, jclass cls, jboolean initialize) {
    (void)env;
    (void)cls;
    return enqueue_task(initialize ? 1 : 0);
}

JNIEXPORT jint JNICALL Java_ArtHeadlessCreateProbe_runContentBootstrapTask(JNIEnv *env, jclass cls) {
    (void)env;
    (void)cls;
    return enqueue_task(2);
}

JNIEXPORT jint JNICALL Java_ArtHeadlessCreateProbe_runCatalogTask(JNIEnv *env, jclass cls) {
    (void)env;
    (void)cls;
    return enqueue_task(3);
}

JNIEXPORT jint JNICALL Java_ArtHeadlessCreateProbe_runGameStateConstructorTask(JNIEnv *env, jclass cls) {
    (void)env;
    (void)cls;
    return enqueue_task(4);
}

JNIEXPORT jint JNICALL Java_ArtHeadlessCreateProbe_runGameStateBattleTask(JNIEnv *env, jclass cls,
                                                                           jbyteArray replay, jint mode) {
    jsize length;
    unsigned char *copy;
    int result;
    (void)cls;
    if (!env || !replay || mode < 5 || mode > 10) return -30;
    length = (*env)->GetArrayLength(env, replay);
    if (length < 2 || length >= 64 * 1024) return -31;
    if (atomic_load_explicit(&busy, memory_order_acquire)) return -32;
    copy = (unsigned char *)malloc((size_t)length + 1);
    if (!copy) return -33;
    (*env)->GetByteArrayRegion(env, replay, 0, length, (jbyte *)copy);
    if ((*env)->ExceptionCheck(env)) {
        (*env)->ExceptionClear(env);
        free(copy);
        return -34;
    }
    copy[length] = '\0';
    battle_json = copy;
    battle_json_size = (size_t)length;
    result = enqueue_task(mode);
    if (result != -5) {
        free(battle_json);
        battle_json = NULL;
        battle_json_size = 0;
    }
    return result;
}
