/* Detach the old Linux root after pivot_root, then exec a real ELF via PT_INTERP. */
#include <errno.h>
#include <grp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/mount.h>
#include <unistd.h>

int main(int argc, char **argv) {
    int program = 1;
    unsigned long uid = 0, gid = 0;
    if (argc >= 5 && strcmp(argv[1], "--user") == 0) {
        char *uid_end, *gid_end;
        errno = 0;
        uid = strtoul(argv[2], &uid_end, 10);
        gid = strtoul(argv[3], &gid_end, 10);
        if (errno || !argv[2][0] || !argv[3][0] || *uid_end || *gid_end ||
            argv[2][0] == '-' || argv[3][0] == '-' || uid >= 0xffffffffUL || gid >= 0xffffffffUL) {
            fputs("invalid numeric uid/gid\n", stderr);
            return 64;
        }
        program = 4;
    }
    if (argc <= program || argv[program][0] != '/' || geteuid() != 0) {
        fputs("namespace_exec requires namespace root and an absolute program\n", stderr);
        return 64;
    }
    FILE *mounts = fopen("/proc/self/mountinfo", "r");
    if (!mounts) {
        perror("mountinfo");
        return 65;
    }
    char line[8192];
    int oldroot_mounted = 0;
    while (fgets(line, sizeof(line), mounts)) {
        if (strstr(line, " /oldroot ")) oldroot_mounted = 1;
    }
    fclose(mounts);
    if (!oldroot_mounted) {
        fputs("pivot_root oldroot mount is required\n", stderr);
        return 66;
    }
    if (umount2("/oldroot", MNT_DETACH) != 0) {
        perror("detach oldroot");
        return 67;
    }
    if (program == 4 && (setgroups(0, NULL) != 0 || setgid((gid_t)gid) != 0 || setuid((uid_t)uid) != 0)) {
        perror("drop child identity");
        return 68;
    }
    execv(argv[program], argv + program);
    int error = errno;
    perror("exec real program");
    return error == ENOENT ? 127 : 126;
}
