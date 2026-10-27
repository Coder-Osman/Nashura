#define _GNU_SOURCE
#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <unistd.h>

#define LIMIT 4095

static int directory = -1;
static pthread_mutex_t mutex = PTHREAD_MUTEX_INITIALIZER;
static const char key_name[] = "LOADING_SCREEN_STATE_LABEL";

typedef struct {
    unsigned char magic[8];
    uint64_t backup_requests;
    uint32_t lengths[2];
    char values[2][LIMIT + 1];
} State;

static int valid(const char *key, const char *value) {
    if (!key || strcmp(key, key_name)) return 0;
    if (!value) return 1;
    size_t length = strnlen(value, LIMIT + 1);
    if (length > LIMIT) return 0;
    for (size_t index = 0; index < length; ++index) {
        if ((unsigned char)value[index] < 32 || (unsigned char)value[index] > 126) return 0;
    }
    return 1;
}

static int load(State *state) {
    int descriptor = openat(directory, "state", O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat information;
    int result = fstat(descriptor, &information);
    ssize_t amount = read(descriptor, state, sizeof(*state));
    int close_result = close(descriptor);
    if (result || close_result || !S_ISREG(information.st_mode) ||
        information.st_size != sizeof(*state) || amount != sizeof(*state) ||
        memcmp(state->magic, "NASHCFG1", 8)) return -1;
    for (unsigned int slot = 0; slot < 2; ++slot) {
        uint32_t length = state->lengths[slot];
        if (length == UINT32_MAX) {
            if (state->values[slot][0]) return -1;
        } else if (length > LIMIT || state->values[slot][length] ||
                   strnlen(state->values[slot], LIMIT + 1) != length ||
                   !valid(key_name, state->values[slot])) return -1;
    }
    return 0;
}

static int save(const State *state) {
    int descriptor = openat(directory, "pending", O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0) return -1;
    const unsigned char *bytes = (const unsigned char *)state;
    size_t written = 0;
    int result = 0;
    while (written < sizeof(*state)) {
        ssize_t amount = write(descriptor, bytes + written, sizeof(*state) - written);
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) { result = -1; break; }
        written += (size_t)amount;
    }
    if (fsync(descriptor)) result = -1;
    if (close(descriptor)) result = -1;
    if (result) return result;
    if (renameat(directory, "pending", directory, "state") || fsync(directory)) return -1;
    return 0;
}

int loading_config_init(const char *path, int fresh) {
    if (directory >= 0 || !path || (fresh != 0 && fresh != 1)) return -1;
    directory = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (directory < 0) return -1;
    State state = {0};
    int result;
    if (fresh) {
        struct stat information;
        if (!fstatat(directory, "state", &information, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) result = -1;
        else {
            memcpy(state.magic, "NASHCFG1", 8);
            state.lengths[0] = state.lengths[1] = UINT32_MAX;
            result = save(&state);
        }
    } else result = load(&state);
    if (result) { close(directory); directory = -1; }
    return result;
}

int loading_config_finish(void) {
    if (directory < 0) return -1;
    int result = close(directory);
    directory = -1;
    return result;
}

static void assign(State *state, unsigned int slot, const char *value) {
    memset(state->values[slot], 0, sizeof(state->values[slot]));
    if (value && *value) {
        state->lengths[slot] = (uint32_t)strlen(value);
        memcpy(state->values[slot], value, state->lengths[slot]);
    } else state->lengths[slot] = UINT32_MAX;
}

int loading_config_write(const char *key, const char *value, uint64_t *backups) {
    if (directory < 0 || !valid(key, value)) return -1;
    pthread_mutex_lock(&mutex);
    State state;
    int result = load(&state);
    int changed = 0;
    for (unsigned int slot = 0; !result && slot < 2; ++slot) {
        if (!value || strcmp(state.values[slot], value)) {
            assign(&state, slot, value);
            changed = 1;
        }
    }
    if (!result && changed) {
        if (state.backup_requests == UINT64_MAX) result = -1;
        else { ++state.backup_requests; result = save(&state); }
    }
    if (!result && backups) *backups = state.backup_requests;
    pthread_mutex_unlock(&mutex);
    return result;
}

int loading_config_read(const char *key, char *value, size_t capacity, uint64_t *backups) {
    if (directory < 0 || !valid(key, NULL) || !value || capacity < LIMIT + 1) return -1;
    pthread_mutex_lock(&mutex);
    State state;
    int result = load(&state);
    if (!result && !state.values[1][0] && state.values[0][0]) {
        assign(&state, 1, state.values[0]);
        if (state.backup_requests == UINT64_MAX) result = -1;
        else { ++state.backup_requests; result = save(&state); }
    }
    if (!result) {
        strcpy(value, state.values[1]);
        if (backups) *backups = state.backup_requests;
    }
    pthread_mutex_unlock(&mutex);
    return result;
}
