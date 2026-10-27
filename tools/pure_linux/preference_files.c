#define _GNU_SOURCE
#include "preference_files.h"

#include <errno.h>
#include <fcntl.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/file.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <unistd.h>

struct PreferenceFiles {
    int directory, lock;
    pid_t process;
    pthread_t thread;
    PreferenceFileObserver observer;
    void *observer_context;
};

static const char *names[] = {"store-0.xml", "store-1.xml"};
static const char *backups[] = {"store-0.xml.bak", "store-1.xml.bak"};

static int owned(const PreferenceFiles *files) {
    return files && files->process == getpid() && pthread_equal(files->thread, pthread_self());
}

static int event(PreferenceFiles *files, enum PreferenceFileEvent stage, unsigned int slot) {
    int result = files->observer ? files->observer(files->observer_context, stage, slot) : 0;
    if (result) errno = result > 0 ? result : EIO;
    return result ? -1 : 0;
}

static int exists(PreferenceFiles *files, const char *name) {
    struct stat status;
    if (fstatat(files->directory, name, &status, AT_SYMLINK_NOFOLLOW)) return errno == ENOENT ? 0 : -1;
    if (!S_ISREG(status.st_mode) || status.st_nlink != 1 || status.st_size < 0 ||
        status.st_size > PREFERENCE_FILE_LIMIT) { errno = EINVAL; return -1; }
    return 1;
}

int preference_files_open(const char *directory, PreferenceFileObserver observer,
                          void *context, PreferenceFiles **output) {
    if (!output) { errno = EINVAL; return -1; }
    *output = NULL;
    if (!directory || directory[0] != '/') { errno = EINVAL; return -1; }
    PreferenceFiles *files = calloc(1, sizeof(*files));
    if (!files) return -1;
    files->directory = open(directory, O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
    if (files->directory < 0) { free(files); return -1; }
    files->lock = openat(files->directory, ".preference.lock", O_RDWR | O_CREAT | O_NOFOLLOW | O_CLOEXEC, 0600);
    struct stat status;
    if (files->lock < 0 || fstat(files->lock, &status) || !S_ISREG(status.st_mode) ||
        status.st_nlink != 1 || flock(files->lock, LOCK_EX | LOCK_NB)) {
        if (files->lock >= 0) close(files->lock);
        close(files->directory);
        free(files);
        return -1;
    }
    files->process = getpid();
    files->thread = pthread_self();
    files->observer = observer;
    files->observer_context = context;
    *output = files;
    return 0;
}

int preference_files_load(PreferenceFiles *files, unsigned int slot, void *output,
                          size_t capacity, size_t *length, int *present) {
    if (!length || !present) return -1;
    *length = 0;
    *present = 0;
    if (!owned(files) || slot > 1 || !output || capacity > PREFERENCE_FILE_LIMIT) return -1;
    int has_backup = exists(files, backups[slot]);
    int has_file = exists(files, names[slot]);
    if (has_backup < 0 || has_file < 0) return -1;
    if (has_backup) {
        if (event(files, PF_RESTORE_BACKUP, slot) ||
            (has_file && unlinkat(files->directory, names[slot], 0)) ||
            renameat(files->directory, backups[slot], files->directory, names[slot])) return -1;
        has_file = 1;
    }
    if (!has_file) return 0;
    int descriptor = openat(files->directory, names[slot], O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (descriptor < 0) return -1;
    struct stat status;
    int result = -1;
    if (fstat(descriptor, &status) || !S_ISREG(status.st_mode) || status.st_nlink != 1 ||
        status.st_size < 0 || (uint64_t)status.st_size > capacity) goto done;
    size_t expected = (size_t)status.st_size, offset = 0;
    while (offset < expected) {
        ssize_t amount = read(descriptor, (char *)output + offset, expected - offset);
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) goto done;
        offset += (size_t)amount;
    }
    char extra;
    ssize_t tail;
    do { tail = read(descriptor, &extra, 1); } while (tail < 0 && errno == EINTR);
    if (tail != 0) goto done;
    *length = expected;
    *present = 1;
    result = 0;
done:
    if (close(descriptor)) result = -1;
    if (result) { *length = 0; *present = 0; }
    return result;
}

int preference_files_commit(PreferenceFiles *files, unsigned int slot, const void *bytes,
                            size_t length, PreferenceFileResult *result) {
    if (!result) return -1;
    memset(result, 0, sizeof(*result));
    if (!owned(files) || slot > 1 || !bytes || !length || length > PREFERENCE_FILE_LIMIT) return -1;
    int has_file = exists(files, names[slot]);
    int has_backup = exists(files, backups[slot]);
    if (has_file < 0 || has_backup < 0) return -1;
    if (has_file) {
        if (!has_backup) {
            if (event(files, PF_RENAME_BACKUP, slot) ||
                renameat(files->directory, names[slot], files->directory, backups[slot])) {
                result->error = errno;
                return 0;
            }
        } else if (unlinkat(files->directory, names[slot], 0)) {
            result->error = errno;
            return -1;
        }
    }
    if (event(files, PF_OPEN_OUTPUT, slot)) { result->error = errno; return 0; }
    int descriptor = openat(files->directory, names[slot], O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0600);
    if (descriptor < 0) { result->error = errno; return 0; }
    struct stat status;
    if (fstat(descriptor, &status) || !S_ISREG(status.st_mode) || status.st_nlink != 1) {
        result->error = EINVAL;
        close(descriptor);
        return -1;
    }
    size_t offset = 0;
    while (offset < length) {
        if (event(files, PF_WRITE, slot)) { result->error = errno; break; }
        ssize_t amount = write(descriptor, (const char *)bytes + offset, length - offset);
        if (amount < 0 && errno == EINTR) continue;
        if (amount <= 0) { result->error = amount ? errno : EIO; break; }
        offset += (size_t)amount;
    }
    if (!result->error && (event(files, PF_SYNC, slot) || fsync(descriptor))) result->sync_error = errno;
    int close_error = event(files, PF_CLOSE_OUTPUT, slot) ? errno : 0;
    if (close(descriptor) && !close_error) close_error = errno;
    if (!result->error) result->error = close_error;
    if (result->error) {
        if (unlinkat(files->directory, names[slot], 0) && errno != ENOENT) return -1;
        return 0;
    }
    if (fchmodat(files->directory, names[slot], 0660, 0)) return -1;
    if (event(files, PF_REMOVE_BACKUP, slot) ||
        (unlinkat(files->directory, backups[slot], 0) && errno != ENOENT))
        result->backup_remove_error = errno;
    result->committed = 1;
    return 0;
}

int preference_files_close(PreferenceFiles *files) {
    if (!owned(files)) return -1;
    int lock_error = close(files->lock), directory_error = close(files->directory);
    free(files);
    return lock_error || directory_error ? -1 : 0;
}
