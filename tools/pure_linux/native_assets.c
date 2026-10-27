#define _GNU_SOURCE
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
#include <unistd.h>

typedef struct {
    char *name;
    int64_t length;
    int compressed;
} Entry;

typedef struct {
    uint64_t magic;
    int descriptor;
    int64_t position;
    const Entry *entry;
} NativeAsset;

static const uint64_t asset_magic = UINT64_C(0x4e41544956454153);
static Entry *entries;
static size_t entry_count;
static int root_descriptor = -1;
static size_t live_assets;
static pthread_mutex_t lock = PTHREAD_MUTEX_INITIALIZER;

static void fail(const char *reason) {
    fprintf(stderr, "NATIVE_ASSET_FATAL\t%s\n", reason);
    fflush(stderr);
    _exit(95);
}

static int valid_path(const char *path) {
    if (!path || !*path || strlen(path) > 4095 || path[0] == '/' ||
        strpbrk(path, "\\:\t\r\n")) return 0;
    const char *component = path;
    while (*component) {
        const char *end = strchr(component, '/');
        size_t length = end ? (size_t)(end - component) : strlen(component);
        if (!length || (length == 1 && component[0] == '.') ||
            (length == 2 && component[0] == '.' && component[1] == '.')) return 0;
        if (!end) return 1;
        component = end + 1;
        if (!*component) return 0;
    }
    return 0;
}

int nashura_assets_init(const char *root, const char *index_path) {
    if (root_descriptor != -1 || entries) return -1;
    FILE *index = fopen(index_path, "r");
    if (!index) return -2;
    char *line = NULL;
    size_t capacity = 0;
    ssize_t amount;
    int status = 0;
    while ((amount = getline(&line, &capacity, index)) >= 0) {
        if (amount > 4200 || amount < 6 || (size_t)amount != strlen(line) ||
            line[amount - 1] != '\n') { status = -3; break; }
        line[amount - 1] = 0;
        char *separator = strchr(line, '\t');
        if (!separator || separator == line) { status = -3; break; }
        *separator = 0;
        int digits = 1;
        for (char *character = line; *character; ++character)
            if (*character < '0' || *character > '9') digits = 0;
        errno = 0;
        char *end;
        unsigned long long parsed = strtoull(line, &end, 10);
        char *flags = separator + 1;
        if (!digits || errno || *end || parsed > INT64_MAX || strlen(flags) < 3 ||
            (flags[0] != '0' && flags[0] != '1') || flags[1] != '\t') {
            status = -3; break;
        }
        char *name = flags + 2;
        int compressed = flags[0] - '0';
        int64_t length = (int64_t)parsed;
        if (!valid_path(name) || entry_count >= 20000) {
            status = -3;
            break;
        }
        for (size_t index_number = 0; index_number < entry_count; ++index_number)
            if (!strcmp(entries[index_number].name, name)) status = -3;
        if (status) break;
        Entry *expanded = realloc(entries, (entry_count + 1) * sizeof(*entries));
        if (!expanded) { status = -4; break; }
        entries = expanded;
        char *copy = strdup(name);
        if (!copy) { status = -4; break; }
        entries[entry_count++] = (Entry){copy, length, compressed};
    }
    if (ferror(index) || !entry_count) status = -3;
    free(line);
    fclose(index);
    if (!status) {
        root_descriptor = open(root, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (root_descriptor < 0) status = -5;
    }
    if (status) {
        for (size_t index_number = 0; index_number < entry_count; ++index_number) free(entries[index_number].name);
        free(entries);
        entries = NULL;
        entry_count = 0;
    } else fprintf(stderr, "NATIVE_ASSET_INIT\t%zu\n", entry_count);
    return status;
}

static int open_relative(const char *name) {
    char *path = strdup(name);
    if (!path) fail("allocation");
    int directory = dup(root_descriptor);
    if (directory < 0) fail("root dup");
    char *component = path;
    char *slash;
    while ((slash = strchr(component, '/'))) {
        *slash = 0;
        int next = openat(directory, component, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        close(directory);
        if (next < 0) { free(path); return -1; }
        directory = next;
        component = slash + 1;
    }
    int descriptor = openat(directory, component, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    close(directory);
    free(path);
    return descriptor;
}

void *AAssetManager_open(void *manager, const char *name, int mode) {
    if (root_descriptor < 0) fail("backend not initialized");
    if (manager) fail("unexpected Android manager in native host");
    if (!valid_path(name) || mode < 0 || mode > 3) { errno = EINVAL; return NULL; }
    const Entry *entry = NULL;
    for (size_t index_number = 0; index_number < entry_count; ++index_number)
        if (!strcmp(entries[index_number].name, name)) { entry = entries + index_number; break; }
    if (!entry) {
        fprintf(stderr, "NATIVE_ASSET_ABSENT_FROM_APK\t%s\n", name);
        errno = ENOENT;
        return NULL;
    }
    int descriptor = open_relative(name);
    if (descriptor < 0) {
        fprintf(stderr, "NATIVE_ASSET_NOT_STAGED\t%s\t%d\n", name, errno);
        fail("indexed asset not available");
    }
    struct stat information;
    if (fstat(descriptor, &information) || !S_ISREG(information.st_mode) ||
        information.st_size != entry->length) fail("indexed asset size/type mismatch");
    NativeAsset *asset = calloc(1, sizeof(*asset));
    if (!asset) fail("asset allocation");
    *asset = (NativeAsset){asset_magic, descriptor, 0, entry};
    pthread_mutex_lock(&lock);
    ++live_assets;
    pthread_mutex_unlock(&lock);
    fprintf(stderr, "NATIVE_ASSET_OPEN\t%s\t%" PRId64 "\n", name, entry->length);
    return asset;
}

static NativeAsset *checked(void *opaque) {
    NativeAsset *asset = opaque;
    if (!asset || asset->magic != asset_magic) fail("invalid native asset handle");
    return asset;
}

int AAsset_read(void *opaque, void *buffer, size_t count) {
    NativeAsset *asset = checked(opaque);
    if (count > INT_MAX || (!buffer && count)) { errno = EINVAL; return -1; }
    int64_t remaining = asset->entry->length - asset->position;
    if (count > (uint64_t)remaining) count = (size_t)remaining;
    ssize_t amount;
    do { amount = pread(asset->descriptor, buffer, count, (off_t)asset->position); }
    while (amount < 0 && errno == EINTR);
    if (amount >= 0) asset->position += amount;
    fprintf(stderr, "NATIVE_ASSET_READ\t%s\t%zd\n", asset->entry->name, amount);
    return (int)amount;
}

int64_t AAsset_seek64(void *opaque, int64_t offset, int origin) {
    NativeAsset *asset = checked(opaque);
    int64_t base_position;
    if (origin == SEEK_SET) base_position = 0;
    else if (origin == SEEK_CUR) base_position = asset->position;
    else if (origin == SEEK_END) base_position = asset->entry->length;
    else { errno = EINVAL; return -1; }
    if (offset < -base_position || offset > asset->entry->length - base_position) {
        errno = EINVAL;
        return -1;
    }
    asset->position = base_position + offset;
    return asset->position;
}

int64_t AAsset_getLength64(void *opaque) { return checked(opaque)->entry->length; }
long AAsset_getLength(void *opaque) { return (long)AAsset_getLength64(opaque); }
int64_t AAsset_getRemainingLength64(void *opaque) {
    NativeAsset *asset = checked(opaque);
    return asset->entry->length - asset->position;
}

int AAsset_openFileDescriptor64(void *opaque, int64_t *start, int64_t *length) {
    NativeAsset *asset = checked(opaque);
    if (!start || !length) { errno = EINVAL; return -1; }
    if (asset->entry->compressed) { errno = ENOTSUP; return -1; }
    int descriptor = dup(asset->descriptor);
    if (descriptor < 0) return -1;
    *start = 0;
    *length = asset->entry->length;
    return descriptor;
}

void AAsset_close(void *opaque) {
    NativeAsset *asset = checked(opaque);
    fprintf(stderr, "NATIVE_ASSET_CLOSE\t%s\n", asset->entry->name);
    if (close(asset->descriptor)) fail("asset close");
    asset->magic = 0;
    free(asset);
    pthread_mutex_lock(&lock);
    --live_assets;
    pthread_mutex_unlock(&lock);
}

int nashura_assets_finish(void) {
    if (root_descriptor < 0 || live_assets) return -1;
    if (close(root_descriptor)) return -2;
    root_descriptor = -1;
    for (size_t index_number = 0; index_number < entry_count; ++index_number) free(entries[index_number].name);
    free(entries);
    entries = NULL;
    entry_count = 0;
    return 0;
}
