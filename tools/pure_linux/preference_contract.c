#include "preference_contract.h"

#include <string.h>

static int valid(const PreferenceBackend *backend, const char *key) {
    return backend && backend->codec && backend->contains && backend->get_string &&
        backend->edit && backend->change && backend->commit &&
        backend->decode_exception && backend->request_backup &&
        key && !strcmp(key, "LOADING_SCREEN_STATE_LABEL");
}

static int transform(const PreferenceBackend *backend, unsigned int slot,
                     enum PreferenceCodec operation, const char *input, char *output) {
    memset(output, 0xff, PREFERENCE_CAPACITY);
    int result = backend->codec(backend->context, slot, operation, input, output, PREFERENCE_CAPACITY);
    if (result) return result;
    return memchr(output, 0, PREFERENCE_CAPACITY) ? 0 : -1;
}

static int contains(const PreferenceBackend *backend, unsigned int slot, const char *key, int *found) {
    *found = -1;
    int result = backend->contains(backend->context, slot, key, found);
    return result || (*found != 0 && *found != 1) ? -1 : 0;
}

static int commit(const PreferenceBackend *backend, uint64_t editor,
                  enum PreferenceEdit operation, const char *key, const char *value) {
    if (backend->change(backend->context, editor, operation, key, value)) return -1;
    int boolean = -1;
    int result = backend->commit(backend->context, editor, &boolean);
    return result || (boolean != 0 && boolean != 1) ? -1 : 0;
}

static int remove_key(const PreferenceBackend *backend, unsigned int slot, const char *key) {
    uint64_t editor = 0;
    if (backend->edit(backend->context, slot, &editor)) return -1;
    return commit(backend, editor, PREFERENCE_REMOVE, key, NULL);
}

static int read_slot(const PreferenceBackend *backend, unsigned int slot, const char *key, char *value) {
    char encoded[PREFERENCE_CAPACITY], stored[PREFERENCE_CAPACITY];
    if (transform(backend, slot, PREFERENCE_KEY, key, encoded)) return -1;
    int found;
    if (contains(backend, slot, encoded, &found)) return -1;
    value[0] = 0;
    if (!found) return 0;
    memset(stored, 0xff, sizeof(stored));
    if (backend->get_string(backend->context, slot, encoded, stored, sizeof(stored)) ||
        !memchr(stored, 0, sizeof(stored))) return -1;
    int result = transform(backend, slot, PREFERENCE_DECRYPT, stored, value);
    if (result == PREFERENCE_DECODE_EXCEPTION) {
        if (backend->decode_exception(backend->context, slot)) return -1;
        value[0] = 0;
    } else if (result) return -1;
    if (!value[0] && remove_key(backend, slot, encoded)) return -1;
    return 0;
}

static int write_slot(const PreferenceBackend *backend, unsigned int slot,
                      const char *key, const char *value) {
    char encoded[PREFERENCE_CAPACITY], encrypted[PREFERENCE_CAPACITY];
    if (transform(backend, slot, PREFERENCE_KEY, key, encoded)) return -1;
    if (value && *value) {
        uint64_t editor = 0;
        if (backend->edit(backend->context, slot, &editor)) return -1;
        if (transform(backend, slot, PREFERENCE_ENCRYPT, value, encrypted)) return -1;
        return commit(backend, editor, PREFERENCE_PUT, encoded, encrypted);
    }
    int found;
    if (contains(backend, slot, key, &found)) return -1;
    if (found && remove_key(backend, slot, key)) return -1;
    if (contains(backend, slot, encoded, &found)) return -1;
    if (found && remove_key(backend, slot, encoded)) return -1;
    return 0;
}

int preference_contract_write(const PreferenceBackend *backend, const char *key, const char *value) {
    if (!valid(backend, key) || (value && strlen(value) >= PREFERENCE_CAPACITY)) return -1;
    int changed = 0;
    char previous[PREFERENCE_CAPACITY];
    for (unsigned int slot = 0; slot < 2; ++slot) {
        if (read_slot(backend, slot, key, previous)) return -1;
        if (!value || strcmp(previous, value)) {
            if (write_slot(backend, slot, key, value)) return -1;
            changed = 1;
        }
    }
    return changed ? (backend->request_backup(backend->context) ? -1 : 0) : 0;
}

int preference_contract_read(const PreferenceBackend *backend, const char *key, char *output, size_t capacity) {
    if (!valid(backend, key) || !output || capacity < PREFERENCE_CAPACITY) return -1;
    char value[PREFERENCE_CAPACITY];
    if (read_slot(backend, 1, key, value)) return -1;
    if (!value[0]) {
        if (read_slot(backend, 0, key, value)) return -1;
        if (value[0] && (write_slot(backend, 1, key, value) ||
                        backend->request_backup(backend->context))) return -1;
    }
    strcpy(output, value);
    return 0;
}
