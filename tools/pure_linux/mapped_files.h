#ifndef NASHURA_MAPPED_FILES_H
#define NASHURA_MAPPED_FILES_H

#ifdef __linux__
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static int mapped_file(struct dl_phdr_info *information, size_t size, void *context) {
    (void)size;
    (void)context;
    FILE *maps = fopen("/proc/self/maps", "r");
    char line[8192];
    int found = 0;
    if (maps) {
        while (fgets(line, sizeof(line), maps)) {
            unsigned long start, end;
            int position = 0;
            if (sscanf(line, "%lx-%lx %*s %*s %*s %*s %n", &start, &end, &position) != 2 ||
                !position || (uintptr_t)information->dlpi_phdr < start ||
                (uintptr_t)information->dlpi_phdr >= end) continue;
            char *path = line + position;
            path[strcspn(path, "\n")] = 0;
            fprintf(stderr, "%s%s\n", path[0] == '/' || !strcmp(path, "[vdso]") ?
                    "mapped:" : "unresolved_mapping:", path);
            found = 1;
            break;
        }
        fclose(maps);
    }
    if (!found) fprintf(stderr, "unresolved_mapping:%s\n", information->dlpi_name);
    return 0;
}

static void observe_mapped_files(void) { dl_iterate_phdr(mapped_file, NULL); }
#else
static void observe_mapped_files(void) {}
#endif

#endif
