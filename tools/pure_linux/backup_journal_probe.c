#include "backup_journal.h"

#include <errno.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

int main(int argument_count, char **arguments) {
    if (argument_count != 3) return 2;
    size_t length = strlen(arguments[2]);
    if (length % 4 || length > 4 * 65536) return 2;
    size_t count = length / 4;
    uint16_t *units = malloc((count ? count : 1) * sizeof(*units));
    if (!units) return 3;
    for (size_t index = 0; index < count; index++) {
        char encoded[5];
        memcpy(encoded, arguments[2] + 4 * index, 4);
        encoded[4] = 0;
        char *end;
        unsigned long value = strtoul(encoded, &end, 16);
        if (*end || strspn(encoded, "0123456789abcdefABCDEF") != 4 || value > 65535) {
            free(units);
            return 2;
        }
        units[index] = (uint16_t)value;
    }
    errno = 0;
    int result = backup_journal_append_utf16(arguments[1], units, count);
    int error = result ? errno : 0;
    free(units);
    printf("{\"result\":%d,\"errno\":%d}\n", result, error);
    return 0;
}
