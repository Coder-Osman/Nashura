#define _POSIX_C_SOURCE 200809L
#include "backup_journal.h"

#include <errno.h>
#include <fcntl.h>
#include <stdlib.h>
#include <sys/stat.h>
#include <unistd.h>

static int finish(int descriptor, int error) {
    int result = close(descriptor);
    if (error) {
        errno = error;
        return -1;
    }
    return result;
}

int backup_journal_append_utf16(const char *path, const uint16_t *units, size_t count) {
    if (!path || (!units && count)) {
        errno = EINVAL;
        return -1;
    }
    int descriptor = open(path, O_RDWR | O_CREAT, 0666);
    if (descriptor < 0) return -1;
    if (fsync(descriptor)) return finish(descriptor, errno);
    struct stat metadata;
    if (fstat(descriptor, &metadata)) return finish(descriptor, errno);
    if (lseek(descriptor, metadata.st_size, SEEK_SET) < 0) return finish(descriptor, errno);
    if (count > 65535) return finish(descriptor, EOVERFLOW);
    size_t length = 0;
    for (size_t index = 0; index < count; index++) {
        uint16_t unit = units[index];
        length += unit && unit <= 0x7f ? 1 : unit <= 0x7ff ? 2 : 3;
        if (length > 65535) return finish(descriptor, EOVERFLOW);
    }
    unsigned char *record = malloc(length + 2);
    if (!record) return finish(descriptor, ENOMEM);
    record[0] = (unsigned char)(length >> 8);
    record[1] = (unsigned char)length;
    size_t offset = 2;
    for (size_t index = 0; index < count; index++) {
        uint16_t unit = units[index];
        if (unit && unit <= 0x7f) record[offset++] = (unsigned char)unit;
        else if (unit <= 0x7ff) {
            record[offset++] = (unsigned char)(0xc0 | unit >> 6);
            record[offset++] = (unsigned char)(0x80 | (unit & 0x3f));
        } else {
            record[offset++] = (unsigned char)(0xe0 | unit >> 12);
            record[offset++] = (unsigned char)(0x80 | ((unit >> 6) & 0x3f));
            record[offset++] = (unsigned char)(0x80 | (unit & 0x3f));
        }
    }
    size_t written = 0;
    int error = 0;
    while (written < length + 2) {
        ssize_t result = write(descriptor, record + written, length + 2 - written);
        if (result < 0 && errno == EINTR) continue;
        if (result <= 0) {
            error = result < 0 ? errno : EIO;
            break;
        }
        written += (size_t)result;
    }
    free(record);
    if (!error && fsync(descriptor)) error = errno;
    return finish(descriptor, error);
}
