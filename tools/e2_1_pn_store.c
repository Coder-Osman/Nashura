#define _POSIX_C_SOURCE 200809L
#define _DARWIN_C_SOURCE
#define _DEFAULT_SOURCE
#include "e2_1_pn_store.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <unistd.h>

struct pn_slot {
    pthread_mutex_t lock;
    char value[E21_PN_TEXT];
    unsigned long sequence;
};

struct pn_store {
    pthread_rwlock_t lifecycle;
    struct pn_slot slots[2];
    int directory;
    int lease;
    int closed;
};

static const char *const files[2] = {"pn-o.bin", "pn-p.bin"};
static const unsigned char magic[8] = {'N', 'S', 'P', 'N', 1, 0, 0, 0};

static int transfer(int descriptor, unsigned char *buffer, size_t count, int writing) {
    while (count) {
        ssize_t amount = writing ? write(descriptor, buffer, count) : read(descriptor, buffer, count);
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) { if (!amount) errno = EIO; return -1; }
        buffer += amount;
        count -= (size_t)amount;
    }
    return 0;
}

static int load_slot(struct pn_store *store, unsigned index) {
    int descriptor = openat(store->directory, files[index], O_RDONLY | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC);
    if (descriptor < 0) return errno == ENOENT ? 0 : E21_PN_IO;
    struct stat status;
    unsigned char bytes[12 + E21_PN_TEXT];
    int result = E21_PN_FORMAT;
    if (fstat(descriptor, &status) || !S_ISREG(status.st_mode)
            || status.st_size < 12 || status.st_size > (off_t)(11 + E21_PN_TEXT)) goto done;
    if (transfer(descriptor, bytes, (size_t)status.st_size, 0)) { result = E21_PN_IO; goto done; }
    uint32_t length = (uint32_t)bytes[8] | (uint32_t)bytes[9] << 8
        | (uint32_t)bytes[10] << 16 | (uint32_t)bytes[11] << 24;
    if (memcmp(bytes, magic, sizeof(magic)) || length >= E21_PN_TEXT
            || status.st_size != (off_t)(12 + length) || memchr(bytes + 12, 0, length)) goto done;
    memcpy(store->slots[index].value, bytes + 12, length);
    store->slots[index].value[length] = 0;
    result = 0;
done:
    close(descriptor);
    return result;
}

static void commit_slot(struct pn_store *store, unsigned index, struct e21_pn_receipt *receipt) {
    struct pn_slot *slot = &store->slots[index];
    uint32_t bit = 1u << index;
    char temporary[96];
    snprintf(temporary, sizeof(temporary), ".pn-%u-%ld-%lu.tmp", index, (long)getpid(), ++slot->sequence);
    int descriptor = openat(store->directory, temporary, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    int error = descriptor < 0 ? errno : 0;
    int created = descriptor >= 0;
    if (!error) {
        size_t length = strlen(slot->value);
        unsigned char bytes[12 + E21_PN_TEXT];
        memcpy(bytes, magic, sizeof(magic));
        for (unsigned shift = 0; shift < 4; ++shift) bytes[8 + shift] = (unsigned char)(length >> (8 * shift));
        memcpy(bytes + 12, slot->value, length);
        if (transfer(descriptor, bytes, length + 12, 1) || fsync(descriptor)) error = errno;
        if (close(descriptor) && !error) error = errno;
        if (!error) {
            if (renameat(store->directory, temporary, store->directory, files[index])) error = errno;
            else {
                receipt->renamed |= bit;
                if (fsync(store->directory)) error = errno;
            }
        }
    }
    if (error) {
        if (created && !(receipt->renamed & bit)) unlinkat(store->directory, temporary, 0);
        receipt->failed |= bit;
        if (index) receipt->error_p = error; else receipt->error_o = error;
    } else receipt->committed |= bit;
}

int e21_pn_open(const char *directory, void **output) {
    if (!directory || !*directory || !output || *output) return E21_PN_INVALID;
    struct pn_store *store = calloc(1, sizeof(*store));
    if (!store) return E21_PN_IO;
    store->directory = store->lease = -1;
    int result = E21_PN_IO;
    int lifecycle = 0;
    unsigned locks = 0;
    if (pthread_rwlock_init(&store->lifecycle, NULL)) goto failed;
    lifecycle = 1;
    for (; locks < 2; ++locks) if (pthread_mutex_init(&store->slots[locks].lock, NULL)) goto failed;
    store->directory = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (store->directory < 0) goto failed;
    store->lease = openat(store->directory, ".pn-lease", O_RDWR | O_CREAT | O_NOFOLLOW | O_NONBLOCK | O_CLOEXEC, 0600);
    struct stat lease_status;
    if (store->lease < 0 || fstat(store->lease, &lease_status) || !S_ISREG(lease_status.st_mode)) goto failed;
    if (flock(store->lease, LOCK_EX | LOCK_NB)) {
        if (errno == EWOULDBLOCK || errno == EAGAIN) result = E21_PN_BUSY;
        goto failed;
    }
    for (unsigned index = 0; index < 2; ++index) {
        result = load_slot(store, index);
        if (result) goto failed;
    }
    *output = store;
    return 0;
failed:
    if (store->lease >= 0) close(store->lease);
    if (store->directory >= 0) close(store->directory);
    while (locks) pthread_mutex_destroy(&store->slots[--locks].lock);
    if (lifecycle) pthread_rwlock_destroy(&store->lifecycle);
    free(store);
    return result;
}

static int begin(void *context, struct e21_pn_receipt *receipt) {
    if (!context || !receipt) return E21_PN_INVALID;
    memset(receipt, 0, sizeof(*receipt));
    struct pn_store *store = context;
    pthread_rwlock_rdlock(&store->lifecycle);
    if (store->closed) {
        pthread_rwlock_unlock(&store->lifecycle);
        return E21_PN_CLOSED;
    }
    return 0;
}

static void read_slot(struct pn_store *store, unsigned index, char *value) {
    pthread_mutex_lock(&store->slots[index].lock);
    strcpy(value, store->slots[index].value);
    pthread_mutex_unlock(&store->slots[index].lock);
}

static int change_slot(struct pn_store *store, unsigned index, const char *value,
                       struct e21_pn_receipt *receipt) {
    char before[E21_PN_TEXT];
    read_slot(store, index, before);
    if (!strcmp(before, value)) return 0;
    pthread_mutex_lock(&store->slots[index].lock);
    strcpy(store->slots[index].value, value);
    receipt->changed |= 1u << index;
    commit_slot(store, index, receipt);
    pthread_mutex_unlock(&store->slots[index].lock);
    return 1;
}

int e21_pn_read(void *context, char *output, size_t capacity, struct e21_pn_receipt *receipt) {
    if (!output || !capacity) return E21_PN_INVALID;
    int result = begin(context, receipt);
    if (result) return result;
    struct pn_store *store = context;
    char value[E21_PN_TEXT];
    read_slot(store, 1, value);
    if (!*value) {
        read_slot(store, 0, value);
        if (*value) {
            pthread_mutex_lock(&store->slots[1].lock);
            strcpy(store->slots[1].value, value);
            receipt->changed |= 2;
            commit_slot(store, 1, receipt);
            pthread_mutex_unlock(&store->slots[1].lock);
            receipt->backup_requested = 1;
        }
    }
    result = strlen(value) + 1 > capacity ? E21_PN_SMALL : 0;
    if (!result) strcpy(output, value);
    pthread_rwlock_unlock(&store->lifecycle);
    return result;
}

int e21_pn_write(void *context, const char *value, struct e21_pn_receipt *receipt) {
    if (value && strnlen(value, E21_PN_TEXT) >= E21_PN_TEXT) return E21_PN_INVALID;
    int result = begin(context, receipt);
    if (result) return result;
    struct pn_store *store = context;
    for (unsigned index = 0; index < 2; ++index) change_slot(store, index, value ? value : "", receipt);
    receipt->backup_requested = receipt->changed != 0;
    pthread_rwlock_unlock(&store->lifecycle);
    return 0;
}

int e21_pn_close(void *context) {
    if (!context) return E21_PN_INVALID;
    struct pn_store *store = context;
    pthread_rwlock_wrlock(&store->lifecycle);
    int result = store->closed ? E21_PN_CLOSED : 0;
    if (!result) {
        store->closed = 1;
        close(store->lease);
        close(store->directory);
    }
    pthread_rwlock_unlock(&store->lifecycle);
    return result;
}

void e21_pn_free(void *context) {
    struct pn_store *store = context;
    if (!store) return;
    e21_pn_close(context);
    for (unsigned index = 0; index < 2; ++index) pthread_mutex_destroy(&store->slots[index].lock);
    pthread_rwlock_destroy(&store->lifecycle);
    free(store);
}
