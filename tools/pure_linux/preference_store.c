#include "preference_store.h"

#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

typedef struct { char *key, *value; } Entry;
typedef struct { Entry entries[PREFERENCE_MAP_LIMIT]; size_t count; int dirty, present; } Map;
typedef struct { const char *cursor, *end; } Reader;

struct PreferenceStore {
    PreferenceFiles *files;
    PreferenceStoreOptions options;
    Map maps[2];
    pid_t process;
    pthread_t thread;
    uint64_t editor;
    unsigned int slot;
    enum PreferenceEdit operation;
    char *key, *value;
    int changed;
    PreferenceStoreSummary summary;
};

static int owned(const PreferenceStore *store) {
    return store && store->process == getpid() && pthread_equal(store->thread, pthread_self());
}

static void clear_map(Map *map) {
    for (size_t index = 0; index < map->count; index++) {
        free(map->entries[index].key);
        free(map->entries[index].value);
    }
    memset(map, 0, sizeof(*map));
}

static size_t find(const Map *map, const char *key) {
    for (size_t index = 0; index < map->count; index++)
        if (!strcmp(map->entries[index].key, key)) return index;
    return map->count;
}

static char *duplicate(const char *value) {
    size_t length = strlen(value);
    if (length >= PREFERENCE_CAPACITY) return NULL;
    char *copy = malloc(length + 1);
    if (copy) memcpy(copy, value, length + 1);
    return copy;
}

static int consume(Reader *reader, const char *literal) {
    size_t length = strlen(literal);
    if ((size_t)(reader->end - reader->cursor) < length || memcmp(reader->cursor, literal, length)) return 0;
    reader->cursor += length;
    return 1;
}

static void space(Reader *reader) {
    while (reader->cursor < reader->end && strchr(" \r\n\t", *reader->cursor)) reader->cursor++;
}

static int scalar(unsigned int value) {
    return value == 9 || value == 10 || value == 13 ||
        (value >= 32 && value <= 0xd7ff) || (value >= 0xe000 && value <= 0xfffd) ||
        (value >= 0x10000 && value <= 0x10ffff);
}

static int append_scalar(char *output, size_t *length, unsigned int value) {
    unsigned char bytes[4];
    size_t amount;
    if (!scalar(value)) return -1;
    if (value < 0x80) { bytes[0] = value; amount = 1; }
    else if (value < 0x800) { bytes[0] = 0xc0 | (value >> 6); bytes[1] = 0x80 | (value & 63); amount = 2; }
    else if (value < 0x10000) {
        bytes[0] = 0xe0 | (value >> 12); bytes[1] = 0x80 | ((value >> 6) & 63);
        bytes[2] = 0x80 | (value & 63); amount = 3;
    } else {
        bytes[0] = 0xf0 | (value >> 18); bytes[1] = 0x80 | ((value >> 12) & 63);
        bytes[2] = 0x80 | ((value >> 6) & 63); bytes[3] = 0x80 | (value & 63); amount = 4;
    }
    if (*length + amount >= PREFERENCE_CAPACITY) return -1;
    memcpy(output + *length, bytes, amount);
    *length += amount;
    return 0;
}

static int utf8(Reader *reader, unsigned int *value) {
    if (reader->cursor == reader->end) return -1;
    unsigned char first = (unsigned char)*reader->cursor++;
    unsigned int number, minimum;
    size_t tail;
    if (first < 128) { *value = first; return scalar(first) ? 0 : -1; }
    if (first >= 0xc2 && first <= 0xdf) { number = first & 31; tail = 1; minimum = 0x80; }
    else if (first >= 0xe0 && first <= 0xef) { number = first & 15; tail = 2; minimum = 0x800; }
    else if (first >= 0xf0 && first <= 0xf4) { number = first & 7; tail = 3; minimum = 0x10000; }
    else return -1;
    if ((size_t)(reader->end - reader->cursor) < tail) return -1;
    for (size_t index = 0; index < tail; index++) {
        unsigned char next = (unsigned char)*reader->cursor++;
        if ((next & 0xc0) != 0x80) return -1;
        number = (number << 6) | (next & 63);
    }
    if (number < minimum || !scalar(number)) return -1;
    *value = number;
    return 0;
}

static int entity(Reader *reader, unsigned int *value) {
    static const char *names[] = {"amp;", "lt;", "gt;", "quot;", "apos;"};
    static const unsigned int values[] = {'&', '<', '>', '"', '\''};
    for (size_t index = 0; index < 5; index++)
        if (consume(reader, names[index])) { *value = values[index]; return 0; }
    if (!consume(reader, "#")) return -1;
    unsigned int base = consume(reader, "x") ? 16 : 10, number = 0, digits = 0;
    while (reader->cursor < reader->end && *reader->cursor != ';') {
        unsigned char byte = *reader->cursor++;
        unsigned int digit;
        if (byte >= '0' && byte <= '9') digit = byte - '0';
        else if (byte >= 'a' && byte <= 'f') digit = byte - 'a' + 10;
        else if (byte >= 'A' && byte <= 'F') digit = byte - 'A' + 10;
        else return -1;
        if (digit >= base || number > (0x10ffff - digit) / base) return -1;
        number = number * base + digit;
        if (++digits > 8) return -1;
    }
    if (!digits || !consume(reader, ";") || !scalar(number)) return -1;
    *value = number;
    return 0;
}

static int text(Reader *reader, char end, int attribute, char **result) {
    char buffer[PREFERENCE_CAPACITY];
    size_t length = 0;
    while (reader->cursor < reader->end && *reader->cursor != end) {
        unsigned int value;
        if (*reader->cursor == '&') {
            reader->cursor++;
            if (entity(reader, &value)) return -1;
        } else {
            if (*reader->cursor == '<' || (!attribute &&
                reader->end - reader->cursor >= 3 && !memcmp(reader->cursor, "]]>", 3))) return -1;
            if (utf8(reader, &value)) return -1;
            if (value == 13) {
                if (reader->cursor < reader->end && *reader->cursor == 10) reader->cursor++;
                value = 10;
            }
            if (attribute && (value == 9 || value == 10)) value = 32;
        }
        if (append_scalar(buffer, &length, value)) return -1;
    }
    if (reader->cursor == reader->end) return -1;
    buffer[length] = 0;
    *result = duplicate(buffer);
    return *result ? 0 : -1;
}

static int parse(Map *map, const char *bytes, size_t length) {
    Reader reader = {bytes, bytes + length};
    if (!length || memchr(bytes, 0, length)) return -1;
    consume(&reader, "\xef\xbb\xbf");
    if (!(consume(&reader, "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>") ||
          consume(&reader, "<?xml version=\"1.0\" encoding=\"utf-8\" standalone=\"yes\" ?>") ||
          consume(&reader, "<?xml version=\"1.0\" encoding=\"UTF-8\"?>"))) {
        if (reader.end - reader.cursor >= 2 && !memcmp(reader.cursor, "<?", 2)) return -1;
    }
    space(&reader);
    if (consume(&reader, "<map />") || consume(&reader, "<map/>")) { space(&reader); return reader.cursor == reader.end ? 0 : -1; }
    if (!consume(&reader, "<map>")) return -1;
    for (;;) {
        space(&reader);
        if (consume(&reader, "</map>")) break;
        if (map->count == PREFERENCE_MAP_LIMIT || !consume(&reader, "<string name=")) return -1;
        char quote = reader.cursor == reader.end ? 0 : *reader.cursor++;
        if (quote != '\'' && quote != '"') return -1;
        char *key = NULL, *value = NULL;
        if (text(&reader, quote, 1, &key)) return -1;
        reader.cursor++;
        int valid = 0;
        if (consume(&reader, " />") || consume(&reader, "/>")) { value = duplicate(""); valid = value != NULL; }
        else if (consume(&reader, ">") && !text(&reader, '<', 0, &value) && consume(&reader, "</string>")) valid = 1;
        if (!valid || find(map, key) != map->count) { free(key); free(value); return -1; }
        map->entries[map->count++] = (Entry){key, value};
    }
    space(&reader);
    return reader.cursor == reader.end ? 0 : -1;
}

static int emit(char *bytes, size_t *length, const char *value) {
    size_t amount = strlen(value);
    if (amount > PREFERENCE_FILE_LIMIT - *length) return -1;
    memcpy(bytes + *length, value, amount);
    *length += amount;
    return 0;
}

static int escaped(char *bytes, size_t *length, const char *value) {
    Reader reader = {value, value + strlen(value)};
    while (reader.cursor < reader.end) {
        const char *start = reader.cursor;
        unsigned int number;
        if (utf8(&reader, &number)) return -1;
        const char *replacement = number == '&' ? "&amp;" : number == '<' ? "&lt;" :
            number == '>' ? "&gt;" : number == '"' ? "&quot;" : number == '\'' ? "&apos;" :
            number == 9 ? "&#9;" : number == 10 ? "&#10;" : number == 13 ? "&#13;" : NULL;
        if (replacement) { if (emit(bytes, length, replacement)) return -1; }
        else {
            size_t amount = (size_t)(reader.cursor - start);
            if (amount > PREFERENCE_FILE_LIMIT - *length) return -1;
            memcpy(bytes + *length, start, amount);
            *length += amount;
        }
    }
    return 0;
}

static int serialize(const Map *map, char *bytes, size_t *length) {
    *length = 0;
    if (emit(bytes, length, "<?xml version='1.0' encoding='utf-8' standalone='yes' ?>\n<map>\n")) return -1;
    for (size_t index = 0; index < map->count; index++) {
        const Entry *entry = &map->entries[index];
        if (emit(bytes, length, "    <string name=\"") || escaped(bytes, length, entry->key) ||
            emit(bytes, length, "\">") || escaped(bytes, length, entry->value) || emit(bytes, length, "</string>\n")) return -1;
    }
    return emit(bytes, length, "</map>\n");
}

static int codec(void *handle, unsigned int slot, enum PreferenceCodec operation,
                  const char *input, char *output, size_t capacity) {
    PreferenceStore *store = handle;
    if (!owned(store) || slot > 1) return -1;
    return store->options.codec(store->options.context, slot, operation, input, output, capacity);
}

static int contains(void *handle, unsigned int slot, const char *key, int *found) {
    PreferenceStore *store = handle;
    if (!owned(store) || slot > 1 || !key || !found) return -1;
    *found = find(&store->maps[slot], key) != store->maps[slot].count;
    return 0;
}

static int get_string(void *handle, unsigned int slot, const char *key, char *output, size_t capacity) {
    PreferenceStore *store = handle;
    if (!owned(store) || slot > 1 || !key || !output) return -1;
    const Map *map = &store->maps[slot];
    size_t index = find(map, key);
    const char *value = index == map->count ? "" : map->entries[index].value;
    if (strlen(value) >= capacity) return -1;
    memcpy(output, value, strlen(value) + 1);
    return 0;
}

static int edit(void *handle, unsigned int slot, uint64_t *token) {
    PreferenceStore *store = handle;
    if (!owned(store) || slot > 1 || !token || store->editor == UINT64_MAX) return -1;
    free(store->key); free(store->value);
    store->key = store->value = NULL;
    store->changed = 0;
    store->slot = slot;
    *token = ++store->editor;
    return 0;
}

static int change(void *handle, uint64_t token, enum PreferenceEdit operation, const char *key, const char *value) {
    PreferenceStore *store = handle;
    if (!owned(store) || !token || token != store->editor || store->changed || !key ||
        (operation != PREFERENCE_PUT && operation != PREFERENCE_REMOVE) ||
        (operation == PREFERENCE_PUT && !value)) return -1;
    char *new_key = duplicate(key), *new_value = value ? duplicate(value) : NULL;
    if (!new_key || (value && !new_value)) { free(new_key); free(new_value); return -1; }
    store->key = new_key; store->value = new_value;
    store->operation = operation;
    store->changed = 1;
    return 0;
}

static int commit(void *handle, uint64_t token, int *boolean) {
    PreferenceStore *store = handle;
    if (!owned(store) || !token || token != store->editor || !store->changed || !boolean) return -1;
    Map *map = &store->maps[store->slot];
    size_t index = find(map, store->key);
    if (store->operation == PREFERENCE_PUT) {
        if (index == map->count) {
            if (map->count == PREFERENCE_MAP_LIMIT) return -1;
            map->entries[map->count++] = (Entry){store->key, store->value};
            store->key = store->value = NULL;
            map->dirty = 1;
        } else if (strcmp(map->entries[index].value, store->value)) {
            free(map->entries[index].value);
            map->entries[index].value = store->value;
            store->value = NULL;
            map->dirty = 1;
        }
    } else if (index < map->count) {
        free(map->entries[index].key); free(map->entries[index].value);
        memmove(map->entries + index, map->entries + index + 1, (map->count - index - 1) * sizeof(Entry));
        map->count--;
        map->dirty = 1;
    }
    store->summary.commits++;
    store->changed = 0;
    *boolean = 1;
    if (map->dirty || !map->present) {
        char bytes[PREFERENCE_FILE_LIMIT];
        size_t length;
        memset(&store->summary.last_file_result, 0, sizeof(store->summary.last_file_result));
        if (serialize(map, bytes, &length)) {
            *boolean = 0;
        } else {
            store->summary.disk_writes++;
            if (preference_files_commit(store->files, store->slot, bytes, length, &store->summary.last_file_result)) return -1;
            *boolean = store->summary.last_file_result.committed;
        }
        if (*boolean) { map->dirty = 0; map->present = 1; }
        else store->summary.false_commits++;
    }
    free(store->key); free(store->value);
    store->key = store->value = NULL;
    return 0;
}

static int exception(void *handle, unsigned int slot) {
    PreferenceStore *store = handle;
    if (!owned(store) || slot > 1) return -1;
    store->summary.decode_exceptions++;
    return store->options.decode_exception(store->options.context, slot);
}

static int backup(void *handle) {
    PreferenceStore *store = handle;
    if (!owned(store)) return -1;
    store->summary.backup_requests++;
    int result = store->options.request_backup(store->options.context);
    if (result) store->summary.backup_failures++;
    return result;
}

int preference_store_open(const char *directory, const PreferenceStoreOptions *options, PreferenceStore **output) {
    if (!output) return -1;
    *output = NULL;
    if (!options || !options->codec || !options->decode_exception || !options->request_backup) return -1;
    PreferenceStore *store = calloc(1, sizeof(*store));
    if (!store) return -1;
    store->options = *options;
    store->process = getpid(); store->thread = pthread_self();
    if (preference_files_open(directory, options->file_observer, options->file_context, &store->files)) {
        free(store); return -1;
    }
    for (unsigned int slot = 0; slot < 2; slot++) {
        char bytes[PREFERENCE_FILE_LIMIT];
        size_t length;
        int present;
        if (preference_files_load(store->files, slot, bytes, sizeof(bytes), &length, &present) ||
            (present && parse(&store->maps[slot], bytes, length))) {
            preference_store_close(store); return -1;
        }
        store->maps[slot].present = present;
    }
    *output = store;
    return 0;
}

int preference_store_backend(PreferenceStore *store, PreferenceBackend *output) {
    if (!owned(store) || !output) return -1;
    *output = (PreferenceBackend){store, codec, contains, get_string, edit, change, commit, exception, backup};
    return 0;
}

int preference_store_summary(PreferenceStore *store, PreferenceStoreSummary *output) {
    if (!owned(store) || !output) return -1;
    *output = store->summary;
    for (size_t slot = 0; slot < 2; slot++) output->entries[slot] = store->maps[slot].count;
    return 0;
}

int preference_store_close(PreferenceStore *store) {
    if (!owned(store)) return -1;
    clear_map(&store->maps[0]); clear_map(&store->maps[1]);
    free(store->key); free(store->value);
    int result = preference_files_close(store->files);
    free(store);
    return result;
}
