#define _GNU_SOURCE
#include <dlfcn.h>
#include <link.h>
#include <stdint.h>
#include <signal.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

static volatile sig_atomic_t proceed = 0;
static void release_probe(int number) { (void)number; proceed = 1; }

static char art_path[4096];

static int vm_library(struct dl_phdr_info *info, size_t size, void *context) {
    (void)size;
    if (strstr(info->dlpi_name, "libart.so") || strstr(info->dlpi_name, "libandroid_runtime.so"))
        *(int *)context = 1;
    if (strstr(info->dlpi_name, "libart.so")) snprintf(art_path, sizeof(art_path), "%s", info->dlpi_name);
    return 0;
}

int main(int argc, char **argv) {
    setvbuf(stdout, NULL, _IONBF, 0);
    if ((argc != 3 && argc != 4) || strcmp(argv[2], "--pinned-preinit") != 0) return 64;
    if (argc == 4 && strcmp(argv[3], "--wait-for-attachment") != 0) return 64;
    signal(SIGUSR1, release_probe);
    puts("{\"event\":\"native_process_started\",\"creates_vm\":false}");
    void *handle = dlopen(argv[1], RTLD_NOW | RTLD_LOCAL);
    if (!handle) {
        fprintf(stderr, "dlopen failed: %s\n", dlerror());
        return 2;
    }
    Dl_info info;
    void *onload = dlsym(handle, "JNI_OnLoad");
    if (!onload || !dladdr(onload, &info)) return 3;
    uintptr_t base = (uintptr_t)info.dli_fbase;
    void *vm = NULL;
    memcpy(&vm, (const void *)(base + 0x1a16b78), sizeof(vm));
    int runtime_loaded = 0;
    dl_iterate_phdr(vm_library, &runtime_loaded);
    void *art = *art_path ? dlopen(art_path, RTLD_NOW | RTLD_NOLOAD) : NULL;
    printf("{\"event\":\"runtime_query_status\",\"art_path\":\"%s\",\"handle_present\":%s}\n", art_path, art ? "true" : "false");
    if (!art) { const char *error = dlerror(); if (error) fprintf(stderr, "ART RTLD_NOLOAD: %s\n", error); }
    int count = -1, query_result = -1;
    void *created[1] = {NULL};
    int (*get_vms)(void **, int, int *) = art ? dlsym(art, "JNI_GetCreatedJavaVMs") : NULL;
    if (get_vms) query_result = get_vms(created, 1, &count);
    printf("{\"event\":\"before_preinit\",\"vm_present\":%s,\"art_or_framework_runtime_loaded\":%s,\"created_vm_count\":%d,\"vm_query_result\":%d}\n",
           vm ? "true" : "false", runtime_loaded ? "true" : "false", count, query_result);
    if (vm || (*art_path && (query_result != 0 || count != 0))) return 4;
    if (argc == 4) {
        puts("{\"event\":\"waiting_for_instrumentation\"}");
        while (!proceed) usleep(10000);
    }
    ((void (*)(void))(base + 0x76cee4))();
    puts("{\"event\":\"original_preinit_returned\",\"battle_initialized\":false}");
    return 5;
}
