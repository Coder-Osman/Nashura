/* Process-global diagnostics only. No allocation counts or leak attribution. */
#ifndef NASHURA_HEADLESS_MEMORY_LIFECYCLE_H
#define NASHURA_HEADLESS_MEMORY_LIFECYCLE_H
#include <malloc.h>

struct memory_mapping_totals {
    unsigned long long maps, size_kb, rss_kb, pss_kb, anonymous_kb, private_dirty_kb;
};

static int memory_mapping_kind(const char *name) {
    if (strstr(name, "dalvik")) return 0;
    if (strstr(name, "libc_malloc") || strstr(name, "scudo") || !strcmp(name, "[heap]")) return 1;
    if (!strncmp(name, "[stack", 6)) return 2;
    if (!*name || !strncmp(name, "[anon:", 6)) return 3;
    if (*name == '/') return 4;
    return 5;
}

/* Bounded stack storage and read(2), avoiding stdio heap buffers for smaps. */
static int memory_read_line(int fd, char *line, size_t capacity,
                            char *buffer, size_t *used, size_t *offset) {
    size_t length = 0;
    for (;;) {
        if (*offset == *used) {
            ssize_t count;
            do { count = read(fd, buffer, 4096); } while (count < 0 && errno == EINTR);
            if (count < 0) return -1;
            if (!count) { line[length] = 0; return length ? 1 : 0; }
            *used = (size_t)count; *offset = 0;
        }
        char value = buffer[(*offset)++];
        if (value == '\n') { line[length] = 0; return 1; }
        if (length + 1 >= capacity) return -1;
        line[length++] = value;
    }
}

static int memory_lifecycle_begin(uint64_t *sequence) {
    static uint64_t next;
    const char *text = getenv("NASHURA_MEMORY_LIFECYCLE_EVERY");
    if (!text) return 0;
    char *end;
    unsigned long interval = strtoul(text, &end, 10);
    if (*text < '0' || *text > '9' || *end || interval < 1 || interval > 1000000) return -1;
    *sequence = next++;
    if (!*sequence) {
        printf("HEADLESS_MEMORY_LIFECYCLE_CONFIG scope=diagnostic-only interval=%lu allocator_api=mallinfo allocator_scope=process-global classification=pathname-heuristic\n", interval);
        fflush(stdout);
    }
    return *sequence % interval == 0;
}

static int memory_lifecycle_sample(uint64_t sequence, const char *phase) {
    static const char *names[] = {"art_named", "allocator_named", "stack_named",
                                  "anonymous_other", "file_named", "special_other"};
    struct memory_mapping_totals groups[6] = {{0}};
    struct timespec start, end;
    if (clock_gettime(CLOCK_MONOTONIC, &start)) return 0;
    /* Read allocator counters before opening /proc. These are process-global,
     * allocator-specific snapshots, not per-step allocation/free counters. */
    struct mallinfo heap = mallinfo();
    char line[4096], buffer[4096];
    size_t used = 0, offset = 0;
    int fd = open("/proc/self/smaps", O_RDONLY | O_CLOEXEC);
    if (fd < 0) return 0;
    int kind = -1, status;
    while ((status = memory_read_line(fd, line, sizeof(line), buffer, &used, &offset)) > 0) {
        unsigned long long start, stop, value;
        char permissions[5];
        int consumed = 0;
        if (sscanf(line, "%llx-%llx %4s %*x %*x:%*x %*u %n",
                   &start, &stop, permissions, &consumed) == 3 && consumed > 0) {
            kind = memory_mapping_kind(line + consumed);
            ++groups[kind].maps;
        } else if (kind >= 0) {
            if (sscanf(line, "Size: %llu kB", &value) == 1) groups[kind].size_kb += value;
            else if (sscanf(line, "Rss: %llu kB", &value) == 1) groups[kind].rss_kb += value;
            else if (sscanf(line, "Pss: %llu kB", &value) == 1) groups[kind].pss_kb += value;
            else if (sscanf(line, "Anonymous: %llu kB", &value) == 1) groups[kind].anonymous_kb += value;
            else if (sscanf(line, "Private_Dirty: %llu kB", &value) == 1) groups[kind].private_dirty_kb += value;
        }
    }
    close(fd);
    if (status < 0 || kind < 0) return 0;
    if (clock_gettime(CLOCK_MONOTONIC, &end)) return 0;
    uint64_t start_ns = (uint64_t)start.tv_sec * UINT64_C(1000000000) + (uint64_t)start.tv_nsec;
    uint64_t end_ns = (uint64_t)end.tv_sec * UINT64_C(1000000000) + (uint64_t)end.tv_nsec;
    printf("HEADLESS_MEMORY_LIFECYCLE scope=diagnostic-only sequence=%llu phase=%s start_ns=%llu end_ns=%llu arena_bytes=%zu hblkhd_bytes=%zu uordblks_bytes=%zu fordblks_bytes=%zu\n",
           (unsigned long long)sequence, phase, (unsigned long long)start_ns,
           (unsigned long long)end_ns, (size_t)heap.arena, (size_t)heap.hblkhd,
           (size_t)heap.uordblks, (size_t)heap.fordblks);
    for (int i = 0; i < 6; ++i) {
        struct memory_mapping_totals *g = groups + i;
        printf("HEADLESS_MEMORY_MAPPING scope=diagnostic-only sequence=%llu phase=%s group=%s maps=%llu size_kb=%llu rss_kb=%llu pss_kb=%llu anonymous_kb=%llu private_dirty_kb=%llu\n",
               (unsigned long long)sequence, phase, names[i], g->maps, g->size_kb,
               g->rss_kb, g->pss_kb, g->anonymous_kb, g->private_dirty_kb);
    }
    fflush(stdout);
    return 1;
}
#endif
