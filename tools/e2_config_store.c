#include <pthread.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

#define E2_CAPACITY 128
#define E2_TEXT 4096

struct e2_entry {
    char key[E2_TEXT];
    char value[E2_TEXT];
    int writable;
};

struct e2_store {
    pthread_mutex_t lock;
    struct e2_entry entries[E2_CAPACITY];
    unsigned count;
    int state;
    uint64_t reads, writes, failures;
};

void *e2_store_create(void) {
    struct e2_store *store = calloc(1, sizeof(*store));
    if (!store) return NULL;
    if (pthread_mutex_init(&store->lock, NULL)) { free(store); return NULL; }
    store->state = 1;
    return store;
}

static int valid(const char *text) { return text && strnlen(text, E2_TEXT) < E2_TEXT; }

static struct e2_entry *lookup(struct e2_store *store, const char *key) {
    for (unsigned index = 0; index < store->count; index++)
        if (strcmp(store->entries[index].key, key) == 0) return &store->entries[index];
    return NULL;
}

int e2_store_add(void *context, const char *key, const char *value, int writable) {
    struct e2_store *store = context;
    if (!store || !valid(key) || !*key || !valid(value) || (writable != 0 && writable != 1)) return -1;
    pthread_mutex_lock(&store->lock);
    int result = -2;
    if (store->state == 1 && store->count < E2_CAPACITY && !lookup(store, key)) {
        struct e2_entry *entry = &store->entries[store->count++];
        strcpy(entry->key, key); strcpy(entry->value, value); entry->writable = writable; result = 0;
    }
    if (result) store->failures++;
    pthread_mutex_unlock(&store->lock);
    return result;
}

int e2_store_seal(void *context) {
    struct e2_store *store = context;
    if (!store) return -1;
    pthread_mutex_lock(&store->lock);
    int result = store->state == 1 && store->count ? 0 : -2;
    if (!result) store->state = 2; else store->failures++;
    pthread_mutex_unlock(&store->lock);
    return result;
}

int e2_store_read(void *context, const char *key, char *output, size_t capacity) {
    struct e2_store *store = context;
    if (!store || !valid(key) || !output || !capacity) return -1;
    pthread_mutex_lock(&store->lock);
    struct e2_entry *entry = lookup(store, key);
    int result = store->state != 2 ? -2 : !entry ? -3 : strlen(entry->value) + 1 > capacity ? -4 : 0;
    if (!result) { strcpy(output, entry->value); store->reads++; } else store->failures++;
    pthread_mutex_unlock(&store->lock);
    return result;
}

int e2_store_write(void *context, const char *key, const char *value) {
    struct e2_store *store = context;
    if (!store || !valid(key) || !valid(value)) return -1;
    pthread_mutex_lock(&store->lock);
    struct e2_entry *entry = lookup(store, key);
    int result = store->state != 2 ? -2 : !entry ? -3 : !entry->writable ? -5 : 0;
    if (!result) { strcpy(entry->value, value); store->writes++; } else store->failures++;
    pthread_mutex_unlock(&store->lock);
    return result;
}

int e2_store_close(void *context) {
    struct e2_store *store = context;
    if (!store) return -1;
    pthread_mutex_lock(&store->lock);
    int result = store->state == 2 ? 0 : -2;
    if (!result) store->state = 3; else store->failures++;
    pthread_mutex_unlock(&store->lock);
    return result;
}

uint64_t e2_store_metric(void *context, unsigned metric) {
    struct e2_store *store = context;
    if (!store) return UINT64_MAX;
    pthread_mutex_lock(&store->lock);
    uint64_t result = metric == 0 ? store->reads : metric == 1 ? store->writes : metric == 2 ? store->failures : UINT64_MAX;
    pthread_mutex_unlock(&store->lock);
    return result;
}

void e2_store_free(void *context) {
    struct e2_store *store = context;
    if (!store) return;
    pthread_mutex_destroy(&store->lock);
    free(store);
}
