#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

extern int loading_config_init(const char *, int);
extern int loading_config_finish(void);
extern int loading_config_write(const char *, const char *, uint64_t *);
extern int loading_config_read(const char *, char *, size_t, uint64_t *);

int main(int count, char **arguments) {
    if (count != 3) return 2;
    int fresh = !strcmp(arguments[2], "fresh");
    int unknown = !strcmp(arguments[2], "unknown");
    if (!fresh && !unknown && strcmp(arguments[2], "restore")) return 2;
    if (loading_config_init(arguments[1], fresh)) return 3;
    const char *key = "LOADING_SCREEN_STATE_LABEL";
    char value[4096];
    uint64_t backups = UINT64_MAX;
    int result = 0;
    if (fresh && (loading_config_write(key, "datatables-init", &backups) || backups != 1)) result = 4;
    if (!result && unknown &&
        (loading_config_write("UNRECOGNIZED", "value", &backups) != -1 ||
         loading_config_read("UNRECOGNIZED", value, sizeof(value), &backups) != -1)) result = 5;
    if (!result && (loading_config_read(key, value, sizeof(value), &backups) ||
                    strcmp(value, "datatables-init") || backups != 1)) result = 6;
    int finished = loading_config_finish();
    if (finished) result = 7;
    printf("CONFIG_PROBE\t%s\tresult=%d\tbackups=%" PRIu64 "\tfinish=%d\n",
           arguments[2], result, backups, finished);
    puts("COVERAGE\tnative candidate persistence only; Android backup/encryption parity unverified");
    return result;
}
