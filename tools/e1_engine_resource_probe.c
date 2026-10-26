#include <stdint.h>
#include <stddef.h>
#include <string.h>
#include <limits.h>

typedef struct { uintptr_t words[3]; } EngineString;

int e1_resolve_path(void *entry, void *destroy, void *filesystem, const char *name,
                    char *output, size_t capacity) {
    if (!entry || !destroy || !filesystem || !name || !output || !capacity) return -3;
    EngineString result = ((EngineString (*)(void *, const char *))entry)(filesystem, name);
    const unsigned char *layout = (const unsigned char *)&result;
    const char *text = layout[0] & 1 ? (const char *)result.words[2] : (const char *)layout + 1;
    size_t length = layout[0] & 1 ? result.words[1] : layout[0] >> 1;
    int status = -1;
    if (length < capacity && length <= INT_MAX && text) {
        memcpy(output, text, length); output[length] = 0; status = (int)length;
    }
    if (layout[0] & 1) ((void (*)(void *))destroy)((void *)result.words[2]);
    return status;
}

int64_t e1_read_resource(void *entry, void *filesystem, const void *name,
                         void *output, int capacity, int64_t *details) {
    if (!details) return -3;
    for (size_t index = 0; index < 4; index++) details[index] = -1;
    if (!entry || !filesystem || !name || !output || capacity < 0) return -3;
    void *file = ((void *(*)(void *, const void *, int))entry)(filesystem, name, 0);
    details[0] = file != NULL;
    if (!file) return -1;
    void **table = *(void ***)file;
    int valid = ((int (*)(void *))table[7])(file);
    details[1] = valid;
    int64_t total = -1;
    if (valid) {
        int64_t length = ((int64_t (*)(void *))table[6])(file);
        details[2] = length;
        if (length >= 0 && length <= capacity) {
            total = 0;
            while (total < length) {
                int64_t amount = ((int64_t (*)(void *, void *, int))table[2])(
                    file, (char *)output + total, (int)(length - total));
                if (amount <= 0 || amount > length - total) { total = -2; break; }
                total += amount;
            }
            details[3] = ((int64_t (*)(void *, void *, int))table[2])(file, output, 1);
        }
    }
    ((void (*)(void *))table[1])(file);
    return total;
}

int e1_exercise_resource(void *entry, void *filesystem, const void *name,
                         int64_t *details) {
    if (!details) return -3;
    for (size_t index = 0; index < 24; index++) details[index] = -1;
    if (!entry || !filesystem || !name) return -3;
    void *file = ((void *(*)(void *, const void *, int))entry)(filesystem, name, 0);
    details[0] = file != NULL;
    if (!file) return -1;
    void **table = *(void ***)file;
    details[1] = ((int (*)(void *))table[7])(file);
    if (!details[1]) {
        ((void (*)(void *))table[1])(file);
        return -1;
    }
    int64_t length = ((int64_t (*)(void *))table[6])(file);
    details[2] = length;
    if (length < 0 || length == INT64_MAX) {
        ((void (*)(void *))table[1])(file);
        return -2;
    }
    const int64_t offsets[] = {0, length, length + 1, -1, length ? length - 1 : 0};
    unsigned char output[8];
    for (size_t index = 0; index < 5; index++) {
        size_t begin = 3 + index * 4;
        details[begin] = ((int64_t (*)(void *, int64_t))table[4])(file, offsets[index]);
        details[begin + 1] = ((int64_t (*)(void *))table[5])(file);
        details[begin + 2] = ((int (*)(void *))table[12])(file);
        details[begin + 3] = ((int64_t (*)(void *, void *, int))table[2])(file, output, 1);
    }
    ((void (*)(void *))table[1])(file);
    return 0;
}
