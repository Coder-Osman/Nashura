/* Run only inside the isolated ART closure, with SDK build.prop as input. */
#include <dlfcn.h>
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/system_properties.h>
#include <unistd.h>

int main(int argc, char **argv) {
    if (argc != 2) return 64;
    struct stat serial;
    if (stat("/oldroot", &serial) != 0 || access("/dev/__properties__/property_info", R_OK)
            || lstat("/dev/__properties__/properties_serial", &serial) == 0) return 65;
    int (*area_init)(void) = dlsym(RTLD_DEFAULT, "__system_property_area_init");
    int (*add)(const char *, unsigned, const char *, unsigned) =
        dlsym(RTLD_DEFAULT, "__system_property_add");
    if (!area_init || !add) return 66;
    errno = 0;
    int result = area_init();
    printf("PROPERTY_AREA_INIT\t%d\tERRNO\t%d\n", result, errno);
    FILE *source = fopen(argv[1], "r");
    if (!source) return 67;
    char *line = NULL;
    size_t capacity = 0;
    int count = 0, failures = 0;
    while (getline(&line, &capacity, source) > 0) {
        char *value = strchr(line, '=');
        if (line[0] == '#' || !value) continue;
        *value++ = '\0';
        value[strcspn(value, "\r\n")] = '\0';
        int status = add(line, (unsigned)strlen(line), value, (unsigned)strlen(value));
        if (status) {
            ++failures;
            printf("PROPERTY_ADD_FAILED\t%s\t%d\n", line, status);
        } else {
            ++count;
        }
    }
    failures += ferror(source) != 0;
    free(line);
    fclose(source);
    char abi[PROP_VALUE_MAX];
    __system_property_get("ro.product.cpu.abilist64", abi);
    printf("PROPERTY_ADDED\t%d\tFAILED\t%d\tABI64\t%s\n", count, failures, abi);
    /* SELinux fsetxattr can fail after usable files were initialized. Record that
       result without claiming Android property service or SELinux equivalence. */
    return count > 0 && failures == 0 && !strcmp(abi, "arm64-v8a") ? 0 : 1;
}
