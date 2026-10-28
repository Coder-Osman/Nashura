#define _POSIX_C_SOURCE 200809L
#include <errno.h>
#include <fcntl.h>
#include <stdarg.h>
#include <stddef.h>
#include <sys/stat.h>
#include <unistd.h>

static int fault;
static int syncs;
static int writes;
static int closes;

void journal_fault_reset(int selected) {
    fault = selected;
    syncs = writes = closes = 0;
}

int journal_close_count(void) { return closes; }
int journal_sync_count(void) { return syncs; }
int journal_write_count(void) { return writes; }

int journal_test_open(const char *path, int flags, ...) {
    if (fault == 1) { errno = EACCES; return -1; }
    va_list arguments;
    va_start(arguments, flags);
    int mode = va_arg(arguments, int);
    va_end(arguments);
    return open(path, flags, mode);
}

int journal_test_fsync(int descriptor) {
    syncs++;
    if ((fault == 2 && syncs == 1) || (fault == 3 && syncs == 2)) {
        errno = EIO;
        return -1;
    }
    return fsync(descriptor);
}

int journal_test_fstat(int descriptor, struct stat *metadata) {
    if (fault == 4) { errno = EIO; return -1; }
    return fstat(descriptor, metadata);
}

off_t journal_test_lseek(int descriptor, off_t offset, int origin) {
    if (fault == 5) { errno = ESPIPE; return -1; }
    return lseek(descriptor, offset, origin);
}

ssize_t journal_test_write(int descriptor, const void *buffer, size_t length) {
    writes++;
    if ((fault == 6 || fault == 10) && writes == 1) { errno = ENOSPC; return -1; }
    if (fault == 7 && writes == 1) { errno = EINTR; return -1; }
    if (fault == 8 && writes == 1 && length > 1) return write(descriptor, buffer, 1);
    if (fault == 9 && writes == 1) return 0;
    if (fault == 12 && writes == 1 && length > 1) return write(descriptor, buffer, 1);
    if (fault == 12 && writes == 2) { errno = ENOSPC; return -1; }
    return write(descriptor, buffer, length);
}

int journal_test_close(int descriptor) {
    closes++;
    int result = close(descriptor);
    if (fault == 10 || fault == 11) { errno = EBADF; return -1; }
    return result;
}
