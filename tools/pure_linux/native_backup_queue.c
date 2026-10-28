#define _GNU_SOURCE
#include "native_backup_queue.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/random.h>
#include <sys/stat.h>
#include <unistd.h>

struct NativeBackupQueue {
    int directory, lock;
    pid_t process;
    pthread_t thread;
};

static const char prefix[] = "nashura.native-backup.request.v1\n";
static const char pending[] = ".backup-pending";
static const char staging[] = ".backup-request.tmp";

static int owned(const NativeBackupQueue *queue) {
    return queue && queue->process == getpid() && pthread_equal(queue->thread, pthread_self());
}

static int private_file(int descriptor) {
    struct stat status;
    return !fstat(descriptor, &status) && S_ISREG(status.st_mode) &&
           status.st_uid == getuid() && status.st_nlink == 1 && (status.st_mode & 0777) == 0600;
}

int native_backup_queue_open(const char *path, NativeBackupQueue **output) {
    if (!output) return -1;
    *output = NULL;
    if (!path || path[0] != '/') return -1;
    NativeBackupQueue *queue = calloc(1, sizeof(*queue));
    if (!queue) return -1;
    queue->directory = open(path, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    queue->lock = -1;
    struct stat status;
    if (queue->directory < 0 || fstat(queue->directory, &status) ||
        status.st_uid != getuid() || (status.st_mode & 0777) != 0700) goto failed;
    queue->lock = openat(queue->directory, ".backup.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (queue->lock < 0 || !private_file(queue->lock) || flock(queue->lock, LOCK_EX | LOCK_NB)) goto failed;
    queue->process = getpid();
    queue->thread = pthread_self();
    *output = queue;
    return 0;
failed:
    if (queue->lock >= 0) close(queue->lock);
    if (queue->directory >= 0) close(queue->directory);
    free(queue);
    return -1;
}

static int read_pending(NativeBackupQueue *queue) {
    int descriptor = openat(queue->directory, pending, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return errno == ENOENT ? 0 : -1;
    char record[sizeof(prefix) - 1 + 33], extra;
    size_t offset = 0;
    int result = -1;
    if (!private_file(descriptor)) goto done;
    while (offset < sizeof(record)) {
        ssize_t count = read(descriptor, record + offset, sizeof(record) - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto done;
        offset += (size_t)count;
    }
    if (read(descriptor, &extra, 1) != 0 || memcmp(record, prefix, sizeof(prefix) - 1) ||
        record[sizeof(record) - 1] != '\n') goto done;
    for (size_t index = sizeof(prefix) - 1; index < sizeof(record) - 1; index++)
        if (!strchr("0123456789abcdef", record[index]) || !record[index]) goto done;
    result = 1;
done:
    if (close(descriptor)) result = -1;
    return result;
}

int native_backup_queue_request(NativeBackupQueue *queue, int *created) {
    if (!owned(queue) || !created) return -1;
    *created = 0;
    struct stat status;
    if (!fstatat(queue->directory, staging, &status, AT_SYMLINK_NOFOLLOW) || errno != ENOENT) return -1;
    int existing = read_pending(queue);
    if (existing < 0) return -1;
    if (existing) return fsync(queue->directory);
    unsigned char random_bytes[16];
    if (getentropy(random_bytes, sizeof(random_bytes))) return -1;
    char record[sizeof(prefix) - 1 + 33 + 1];
    memcpy(record, prefix, sizeof(prefix) - 1);
    for (size_t index = 0; index < sizeof(random_bytes); index++)
        snprintf(record + sizeof(prefix) - 1 + index * 2, 3, "%02x", random_bytes[index]);
    record[sizeof(record) - 2] = '\n';
    int descriptor = openat(queue->directory, staging, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0) return -1;
    int result = -1;
    size_t offset = 0;
    while (offset < sizeof(record) - 1) {
        ssize_t count = write(descriptor, record + offset, sizeof(record) - 1 - offset);
        if (count < 0 && errno == EINTR) continue;
        if (count <= 0) goto done;
        offset += (size_t)count;
    }
    if (!private_file(descriptor) || fsync(descriptor)) goto done;
    if (linkat(queue->directory, staging, queue->directory, pending, 0) || fsync(queue->directory) ||
        unlinkat(queue->directory, staging, 0) || fsync(queue->directory)) goto done;
    *created = 1;
    result = 0;
done:
    if (close(descriptor)) result = -1;
    return result;
}

int native_backup_queue_close(NativeBackupQueue *queue) {
    if (!owned(queue)) return -1;
    int result = close(queue->lock);
    if (close(queue->directory)) result = -1;
    free(queue);
    return result;
}
