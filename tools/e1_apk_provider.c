#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <errno.h>
#include <fcntl.h>
#include <inttypes.h>
#include <limits.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>
#include <zlib.h>

typedef struct {
    char *name;
    int64_t offset, length, packed;
    unsigned method;
    uint32_t crc;
} Entry;
typedef struct {
    Entry *entries;
    size_t count;
    int apk;
    FILE *audit;
    pthread_mutex_t lock;
    uint64_t calls, failures, live;
} Provider;
typedef struct {
    Provider *provider;
    const Entry *entry;
    unsigned char *data;
    int64_t position;
} Asset;
static Provider *active;

static int valid_path(const char *name) {
    if (!name || !*name || *name == '/' || strchr(name, '\t') ||
        strchr(name, '\n') || strchr(name, '\r') || strchr(name, '\\')) return 0;
    const char *component = name;
    for (const char *cursor = name;; cursor++) {
        if (*cursor == '/' || !*cursor) {
            size_t length = (size_t)(cursor - component);
            if (!length || (length == 1 && component[0] == '.') ||
                (length == 2 && component[0] == '.' && component[1] == '.')) return 0;
            component = cursor + 1;
        }
        if (!*cursor) break;
    }
    return 1;
}

static void audit(Provider *provider, const char *operation, const char *name,
                  int64_t argument, int64_t result, int failure) {
    pthread_mutex_lock(&provider->lock);
    provider->calls++;
    provider->failures += failure != 0;
    fprintf(provider->audit, "%s\t%s\t%" PRId64 "\t%" PRId64 "\t%d\n",
            operation, valid_path(name) ? name : "<invalid>", argument, result, failure);
    fflush(provider->audit);
    pthread_mutex_unlock(&provider->lock);
}

static int compare_entry(const void *left, const void *right) {
    return strcmp(((const Entry *)left)->name, ((const Entry *)right)->name);
}

void *e1_initialize(const char *apk, const char *index, const char *audit_path) {
    if (active) { errno = EALREADY; return NULL; }
    FILE *input = fopen(index, "r");
    if (!input) return NULL;
    Provider *provider = calloc(1, sizeof(*provider));
    if (!provider) { fclose(input); return NULL; }
    provider->apk = open(apk, O_RDONLY | O_CLOEXEC);
    provider->audit = fopen(audit_path, "wx");
    struct stat status;
    int okay = provider->apk >= 0 && provider->audit && fstat(provider->apk, &status) == 0;
    char *line = NULL;
    size_t capacity = 0;
    while (okay && getline(&line, &capacity, input) >= 0) {
        char *fields[7], *save = NULL;
        size_t count = 0;
        for (char *token = strtok_r(line, "\t\n", &save); token && count < 7;
             token = strtok_r(NULL, "\t\n", &save)) fields[count++] = token;
        if (count != 6 || !valid_path(fields[0])) { okay = 0; break; }
        Entry entry = {.name = strdup(fields[0])};
        char *end = NULL;
        int64_t numbers[5];
        for (size_t column = 0; column < 5; column++) {
            errno = 0;
            numbers[column] = strtoll(fields[column + 1], &end, 10);
            if (errno || *end || numbers[column] < 0) okay = 0;
        }
        entry.offset = numbers[0]; entry.length = numbers[1]; entry.packed = numbers[2];
        entry.method = (unsigned)numbers[3]; entry.crc = (uint32_t)numbers[4];
        if (!entry.name || entry.offset > status.st_size ||
            entry.packed > status.st_size - entry.offset ||
            entry.length > INT_MAX || entry.packed > INT_MAX ||
            (numbers[3] != 0 && numbers[3] != 8) || numbers[4] > UINT32_MAX ||
            (!entry.method && entry.length != entry.packed)) okay = 0;
        if (!okay) { free(entry.name); break; }
        Entry *grown = realloc(provider->entries, (provider->count + 1) * sizeof(Entry));
        if (!grown) { free(entry.name); okay = 0; break; }
        provider->entries = grown;
        provider->entries[provider->count++] = entry;
    }
    if (ferror(input)) okay = 0;
    free(line); fclose(input);
    if (provider->count) qsort(provider->entries, provider->count, sizeof(Entry), compare_entry);
    for (size_t position = 1; position < provider->count; position++)
        if (!strcmp(provider->entries[position-1].name, provider->entries[position].name)) okay = 0;
    if (!okay || !provider->count || pthread_mutex_init(&provider->lock, NULL)) {
        for (size_t position = 0; position < provider->count; position++) free(provider->entries[position].name);
        free(provider->entries);
        if (provider->apk >= 0) close(provider->apk);
        if (provider->audit) fclose(provider->audit);
        free(provider); errno = EINVAL; return NULL;
    }
    active = provider;
    return provider;
}

void *e1_AAssetManager_fromJava(void *environment, void *java_manager) {
    (void)environment; (void)java_manager;
    if (!active) { errno = ENODEV; return NULL; }
    audit(active, "bind", "<context>", 0, 1, 0);
    return active;
}

void *e1_AAssetManager_open(void *manager, const char *name, int mode) {
    Provider *provider = active;
    if (!provider) { errno = ENODEV; return NULL; }
    if (manager != provider || !valid_path(name) || mode < 0 || mode > 3) {
        audit(provider, "invalid_open", name, mode, -1, 1); errno = EINVAL; return NULL;
    }
    Entry key = {.name = (char *)name};
    const Entry *entry = bsearch(&key, provider->entries, provider->count, sizeof(Entry), compare_entry);
    if (!entry) { audit(provider, "missing", name, mode, 0, 0); errno = ENOENT; return NULL; }
    Asset *asset = calloc(1, sizeof(*asset));
    if (!asset) {
        audit(provider, "open_error", name, mode, -1, 1); return NULL;
    }
    asset->provider = provider; asset->entry = entry;
    pthread_mutex_lock(&provider->lock); provider->live++; pthread_mutex_unlock(&provider->lock);
    audit(provider, "open", name, mode, entry->length, 0);
    return asset;
}

static int materialize(Asset *asset) {
    if (asset->data) return 1;
    Provider *provider = asset->provider;
    const Entry *entry = asset->entry;
    unsigned char *packed = malloc((size_t)entry->packed + 1);
    asset->data = malloc((size_t)entry->length + 1);
    int okay = asset->data && packed;
    int64_t received = 0;
    while (okay && received < entry->packed) {
        ssize_t amount = pread(provider->apk, packed + received, (size_t)(entry->packed - received), entry->offset + received);
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) { okay = 0; break; }
        received += amount;
    }
    if (okay && entry->method == 8) {
        z_stream stream = {0};
        stream.next_in = packed; stream.avail_in = (uInt)entry->packed;
        stream.next_out = asset->data; stream.avail_out = (uInt)entry->length + 1;
        if (inflateInit2(&stream, -MAX_WBITS) != Z_OK) okay = 0;
        else {
            okay = inflate(&stream, Z_FINISH) == Z_STREAM_END &&
                   stream.total_out == (uLong)entry->length && stream.total_in == (uLong)entry->packed;
            inflateEnd(&stream);
        }
    } else if (okay) memcpy(asset->data, packed, (size_t)entry->length);
    free(packed);
    if (!okay) {
        free(asset->data); asset->data = NULL;
        audit(provider, "read_error", entry->name, asset->position, -1, 1);
        errno = EIO; return 0;
    }
    return 1;
}

int e1_AAsset_read(void *handle, void *buffer, size_t count) {
    Asset *asset = handle;
    if (count > INT_MAX || (!buffer && count)) {
        audit(asset->provider, "invalid_read", asset->entry->name, (int64_t)count, -1, 1);
        errno = EINVAL; return -1;
    }
    if (!materialize(asset)) return -1;
    int64_t amount = asset->entry->length - asset->position;
    if ((uint64_t)amount > count) amount = (int64_t)count;
    if (amount) memcpy(buffer, asset->data + asset->position, (size_t)amount);
    asset->position += amount;
    audit(asset->provider, "read", asset->entry->name, (int64_t)count, amount, 0);
    return (int)amount;
}

int64_t e1_AAsset_seek64(void *handle, int64_t offset, int whence) {
    Asset *asset = handle;
    int64_t base = whence == SEEK_SET ? 0 : whence == SEEK_CUR ? asset->position : asset->entry->length;
    if ((whence != SEEK_SET && whence != SEEK_CUR && whence != SEEK_END) ||
        offset < -base || offset > asset->entry->length - base) {
        audit(asset->provider, "seek", asset->entry->name, offset, -1, 0); errno = EINVAL; return -1;
    }
    asset->position = base + offset;
    audit(asset->provider, "seek", asset->entry->name, offset, asset->position, 0);
    return asset->position;
}

int64_t e1_AAsset_getLength64(void *handle) {
    Asset *asset = handle;
    audit(asset->provider, "length", asset->entry->name, 0, asset->entry->length, 0);
    return asset->entry->length;
}

long e1_AAsset_getLength(void *handle) { return (long)e1_AAsset_getLength64(handle); }

int64_t e1_AAsset_getRemainingLength64(void *handle) {
    Asset *asset = handle;
    int64_t result = asset->entry->length - asset->position;
    audit(asset->provider, "remaining", asset->entry->name, 0, result, 0);
    return result;
}

int e1_AAsset_openFileDescriptor64(void *handle, int64_t *start, int64_t *length) {
    Asset *asset = handle;
    int result = -1;
    if (!asset->entry->method && start && length) {
        result = dup(asset->provider->apk);
        if (result >= 0) { *start = asset->entry->offset; *length = asset->entry->length; }
    }
    audit(asset->provider, "descriptor", asset->entry->name, 0, result >= 0, 0);
    return result;
}

void e1_AAsset_close(void *handle) {
    Asset *asset = handle;
    audit(asset->provider, "close", asset->entry->name, 0, 0, 0);
    pthread_mutex_lock(&asset->provider->lock); asset->provider->live--; pthread_mutex_unlock(&asset->provider->lock);
    free(asset->data); free(asset);
}

uint64_t e1_metric(unsigned metric) {
    if (!active) return UINT64_MAX;
    pthread_mutex_lock(&active->lock);
    uint64_t value = metric == 0 ? active->calls : metric == 1 ? active->failures :
                     metric == 2 ? active->live : active->count;
    pthread_mutex_unlock(&active->lock);
    return value;
}
