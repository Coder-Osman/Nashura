#include "native_backup_queue.h"

#include <stdio.h>
#include <string.h>
#include <unistd.h>

static int crash_after_link;

int __real_linkat(int, const char *, int, const char *, int);

int __wrap_linkat(int olddir, const char *oldname, int newdir, const char *newname, int flags) {
    int result = __real_linkat(olddir, oldname, newdir, newname, flags);
    if (!result && crash_after_link) _exit(71);
    return result;
}

int main(int argc, char **argv) {
    if (argc != 3 || (strcmp(argv[2], "crash") && strcmp(argv[2], "request"))) return 2;
    crash_after_link = !strcmp(argv[2], "crash");
    NativeBackupQueue *queue = NULL;
    int created = -1;
    if (native_backup_queue_open(argv[1], &queue)) return 3;
    int result = native_backup_queue_request(queue, &created);
    int close_result = native_backup_queue_close(queue);
    printf("request_result=%d created=%d close_result=%d\n", result, created, close_result);
    return result || close_result ? 4 : 0;
}
