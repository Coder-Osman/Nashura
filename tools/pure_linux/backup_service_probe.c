#define _GNU_SOURCE
#include "mapped_files.h"

#include <android/binder_ibinder.h>
#include <dlfcn.h>
#include <stdio.h>

int main(int argument_count, char **arguments) {
    if (argument_count != 2) return 2;
    setvbuf(stdout, NULL, _IONBF, 0);
    puts("BACKUP_PHASE\tbefore_binder_load");
    void *library = dlopen(arguments[1], RTLD_NOW | RTLD_LOCAL);
    if (!library) {
        fprintf(stderr, "BACKUP_LOAD_ERROR\t%s\n", dlerror());
        return 95;
    }
    puts("BACKUP_PHASE\tafter_binder_load");
    observe_mapped_files();
    dlerror();
    AIBinder *(*check_service)(const char *) = dlsym(library, "AServiceManager_checkService");
    const char *error = dlerror();
    if (error || !check_service) {
        fprintf(stderr, "BACKUP_SYMBOL_ERROR\t%s\n", error ? error : "null symbol");
        return 95;
    }
    puts("BACKUP_PHASE\tbefore_check_service");
    AIBinder *service = check_service("backup");
    puts("BACKUP_PHASE\tafter_check_service");
    if (!service) {
        puts("BACKUP_DISCOVERY\tservice_absent_not_contract_success");
        return dlclose(library) ? 95 : 93;
    }
    dlerror();
    void (*release)(AIBinder *) = dlsym(library, "AIBinder_decStrong");
    error = dlerror();
    if (error || !release) {
        fprintf(stderr, "BACKUP_SYMBOL_ERROR\t%s\n", error ? error : "null release");
        return 95;
    }
    release(service);
    puts("BACKUP_DISCOVERY\tservice_found_contract_not_exercised");
    return dlclose(library) ? 95 : 94;
}
