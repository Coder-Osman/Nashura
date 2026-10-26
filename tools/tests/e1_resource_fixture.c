#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include "../e1_engine_resource_probe.c"

static int destroyed;
static int released;
static int64_t length = 3;
static int64_t cursor;
static int valid = 1;
static int read_error;
static void *table[13];
static struct { void **vtable; } file;

static void close_file(void *self) { (void)self; destroyed++; }
static int validity(void *self) { (void)self; return valid; }
static int64_t file_length(void *self) { (void)self; return length; }
static int64_t tell(void *self) { (void)self; return cursor; }
static int eof(void *self) { (void)self; return cursor >= length; }
static int64_t seek(void *self, int64_t position) {
    (void)self;
    if (position < 0 || position > length) return -1;
    cursor = position; return cursor;
}
static int64_t read_file(void *self, void *output, int amount) {
    (void)self;
    if (read_error) return -1;
    int64_t available = length - cursor;
    if (available < amount) amount = (int)available;
    memset(output, 0x31, (size_t)amount); cursor += amount; return amount;
}
static void *open_file(void *filesystem, const void *name, int mode) {
    (void)filesystem; (void)mode;
    if (!strcmp(name, "missing")) return NULL;
    cursor = 0;
    table[1] = close_file; table[2] = read_file; table[4] = seek;
    table[5] = tell; table[6] = file_length; table[7] = validity; table[12] = eof;
    file.vtable = table; return &file;
}
static EngineString resolve(void *filesystem, const char *name) {
    (void)filesystem;
    EngineString result = {{0, 0, 0}};
    if (strlen(name) < 22) {
        unsigned char *bytes = (unsigned char *)&result;
        bytes[0] = (unsigned char)(strlen(name) << 1);
        memcpy(bytes + 1, name, strlen(name));
    } else {
        result.words[0] = 1; result.words[1] = strlen(name);
        result.words[2] = (uintptr_t)strdup(name);
    }
    return result;
}
static void release(void *text) { released++; free(text); }

int fixture_read(const char *name, int64_t size, int error, int validity_flag, int64_t *details) {
    unsigned char output[8];
    length = size; read_error = error; valid = validity_flag; destroyed = 0;
    return (int)e1_read_resource(open_file, &file, name, output, sizeof(output), details);
}
int fixture_exercise(int64_t size, int64_t *details) {
    length = size; read_error = 0; valid = 1; destroyed = 0;
    return e1_exercise_resource(open_file, &file, "present", details);
}
int fixture_resolve(const char *name, char *output, size_t capacity) {
    released = 0; return e1_resolve_path(resolve, release, &file, name, output, capacity);
}
int fixture_destroyed(void) { return destroyed; }
int fixture_released(void) { return released; }
