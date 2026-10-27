#include "preference_contract.h"

#include <stdio.h>
#include <string.h>

#define CHECK(condition) do { if (!(condition)) { \
    fprintf(stderr, "check failed at line %d\n", __LINE__); return 2; } } while (0)

typedef struct {
    int present[2], legacy[2], disk_present[2], type_error[2];
    char value[2][128], disk_value[2][128];
    struct { unsigned int slot; enum PreferenceEdit operation; int legacy; char value[128]; } editors[16];
    int events, fail_at, false_commit, commits, backups, exceptions;
    uint64_t next_editor;
} Context;

static const char *key = "LOADING_SCREEN_STATE_LABEL";

static int event(Context *context) {
    return ++context->events == context->fail_at ? -1 : 0;
}

static int copy(const char *input, char *output, size_t capacity) {
    if (!input || strlen(input) >= capacity) return -1;
    strcpy(output, input);
    return 0;
}

static int codec(void *opaque, unsigned int slot, enum PreferenceCodec operation,
                 const char *input, char *output, size_t capacity) {
    Context *context = opaque;
    if (slot > 1) return -1;
    if (event(context)) return operation == PREFERENCE_DECRYPT ? PREFERENCE_DECODE_EXCEPTION : -1;
    if (operation == PREFERENCE_KEY) {
        int size = snprintf(output, capacity, "e%u:%s", slot, input);
        return size < 0 || (size_t)size >= capacity ? -1 : 0;
    }
    if (operation == PREFERENCE_ENCRYPT) {
        int size = snprintf(output, capacity, "v:%s", input);
        return size < 0 || (size_t)size >= capacity ? -1 : 0;
    }
    if (operation != PREFERENCE_DECRYPT) return -1;
    if (strncmp(input, "v:", 2)) return PREFERENCE_DECODE_EXCEPTION;
    return copy(input + 2, output, capacity);
}

static int contains(void *opaque, unsigned int slot, const char *input, int *found) {
    Context *context = opaque;
    if (slot > 1 || event(context)) return -1;
    *found = !strcmp(input, key) ? context->legacy[slot] : context->present[slot];
    return 0;
}

static int get(void *opaque, unsigned int slot, const char *input, char *output, size_t capacity) {
    Context *context = opaque;
    if (slot > 1 || !input || event(context) || context->type_error[slot]) return -1;
    return copy(context->value[slot], output, capacity);
}

static int edit(void *opaque, unsigned int slot, uint64_t *token) {
    Context *context = opaque;
    if (slot > 1 || event(context) || context->next_editor == 15) return -1;
    *token = ++context->next_editor;
    context->editors[*token].slot = slot;
    return 0;
}

static int change(void *opaque, uint64_t token, enum PreferenceEdit operation,
                  const char *input, const char *value) {
    Context *context = opaque;
    if (!token || token > context->next_editor || event(context)) return -1;
    context->editors[token].operation = operation;
    context->editors[token].legacy = !strcmp(input, key);
    if (operation == PREFERENCE_PUT) return copy(value, context->editors[token].value, 128);
    return operation == PREFERENCE_REMOVE ? 0 : -1;
}

static int commit(void *opaque, uint64_t token, int *boolean) {
    Context *context = opaque;
    if (!token || token > context->next_editor || event(context)) return -1;
    unsigned int slot = context->editors[token].slot;
    ++context->commits;
    if (context->editors[token].legacy) context->legacy[slot] = 0;
    else if (context->editors[token].operation == PREFERENCE_REMOVE) context->present[slot] = 0;
    else {
        context->present[slot] = 1;
        if (copy(context->editors[token].value, context->value[slot], 128)) return -1;
    }
    *boolean = context->commits != context->false_commit;
    if (*boolean) {
        context->disk_present[slot] = context->present[slot];
        if (copy(context->value[slot], context->disk_value[slot], 128)) return -1;
    }
    return 0;
}

static int exception(void *opaque, unsigned int slot) {
    Context *context = opaque;
    if (slot > 1 || event(context)) return -1;
    ++context->exceptions;
    return 0;
}

static int backup(void *opaque) {
    Context *context = opaque;
    if (event(context)) return -1;
    ++context->backups;
    return 0;
}

static PreferenceBackend backend(Context *context) {
    PreferenceBackend result = {context, codec, contains, get, edit, change, commit, exception, backup};
    return result;
}

int main(void) {
    Context context = {.false_commit = 1};
    PreferenceBackend callbacks = backend(&context);
    CHECK(preference_contract_write(&callbacks, key, "new") == 0);
    CHECK(context.commits == 2 && context.backups == 1);
    CHECK(context.present[0] && context.present[1] && !context.disk_present[0] && context.disk_present[1]);
    int normal_events = context.events;
    for (int boundary = 1; boundary <= normal_events; ++boundary) {
        memset(&context, 0, sizeof(context));
        context.fail_at = boundary;
        CHECK(preference_contract_write(&callbacks, key, "new") == -1);
        CHECK(context.events == boundary);
        if (boundary == normal_events) CHECK(context.disk_present[0] && context.disk_present[1]);
    }
    memset(&context, 0, sizeof(context));
    context.present[0] = context.present[1] = context.legacy[0] = context.legacy[1] = 1;
    strcpy(context.value[0], "v:old");
    strcpy(context.value[1], "v:old");
    CHECK(preference_contract_write(&callbacks, key, NULL) == 0);
    CHECK(context.commits == 4 && context.backups == 1);
    CHECK(!context.present[0] && !context.present[1] && !context.legacy[0] && !context.legacy[1]);
    memset(&context, 0, sizeof(context));
    context.present[0] = context.present[1] = 1;
    strcpy(context.value[0], "v:recovered");
    strcpy(context.value[1], "broken");
    char output[PREFERENCE_CAPACITY];
    CHECK(preference_contract_read(&callbacks, key, output, sizeof(output)) == 0);
    CHECK(!strcmp(output, "recovered") && !strcmp(context.value[1], "v:recovered"));
    CHECK(context.exceptions == 1 && context.commits == 2 && context.backups == 1);
    memset(&context, 0, sizeof(context));
    context.present[1] = context.type_error[1] = 1;
    CHECK(preference_contract_read(&callbacks, key, output, sizeof(output)) == -1);
    CHECK(!context.exceptions && !context.commits && !context.backups);
    memset(&context, 0, sizeof(context));
    context.present[1] = 1;
    context.fail_at = 4;
    strcpy(context.value[1], "v:old");
    CHECK(preference_contract_read(&callbacks, key, output, sizeof(output)) == 0);
    CHECK(!output[0] && context.exceptions == 1 && context.commits == 1 && !context.present[1]);
    memset(&context, 0, sizeof(context));
    context.legacy[0] = context.legacy[1] = 1;
    CHECK(preference_contract_write(&callbacks, key, "") == 0);
    CHECK(!context.commits && !context.backups && context.legacy[0] && context.legacy[1]);
    CHECK(preference_contract_write(&callbacks, "UNKNOWN", "value") == -1);
    printf("{\"status\":\"PASS\",\"scope\":\"synthetic_backend_only\",\"failure_boundaries\":%d,"
           "\"scenario_groups\":8,\"libg_loaded\":false}\n", normal_events);
    return 0;
}
