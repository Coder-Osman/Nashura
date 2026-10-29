/* Observe the investigated engine's VM slot/getter after Java System.load. */
#define _GNU_SOURCE
#include <inttypes.h>
#include <jni.h>
#include <link.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>

static uintptr_t engine_base;

static int readable(uintptr_t address, size_t length) {
    if (!address || address + length < address) return 0;
    FILE *stream = fopen("/proc/self/maps", "r");
    if (!stream) return 0;
    char line[1024], permissions[5];
    uintptr_t start, end;
    int result = 0;
    while (fgets(line, sizeof(line), stream)) {
        if (sscanf(line, "%" SCNxPTR "-%" SCNxPTR " %4s", &start, &end, permissions) == 3
                && permissions[0] == 'r' && start <= address && address + length <= end) {
            result = 1;
            break;
        }
    }
    fclose(stream);
    return result;
}

static int mapping(struct dl_phdr_info *info, size_t size, void *opaque) {
    (void)size;
    (void)opaque;
    const char *name = strrchr(info->dlpi_name, '/');
    name = name ? name + 1 : info->dlpi_name;
    if (!strcmp(name, "libg.so")) engine_base = (uintptr_t)info->dlpi_addr;
    return 0;
}

JNIEXPORT jint JNICALL Java_ArtEngineLoadProbe_inspect(JNIEnv *env, jclass cls) {
    (void)cls;
    engine_base = 0;
    dl_iterate_phdr(mapping, NULL);
    uintptr_t slot = engine_base + 0x1a16b78;
    uintptr_t getter = engine_base + 0x17d8ae8;
    static const unsigned char prologue[16] = {
        0xff, 0xc3, 0x00, 0xd1, 0xfd, 0x7b, 0x01, 0xa9,
        0xf3, 0x13, 0x00, 0xf9, 0xfd, 0x43, 0x00, 0x91
    };
    if (!engine_base || !readable(slot, sizeof(JavaVM *)) || !readable(getter, 16)
            || memcmp((const void *)getter, prologue, sizeof(prologue))) return 0;
    unsigned mask = 1;
    JavaVM *actual = NULL, *bound = NULL;
    memcpy(&bound, (const void *)slot, sizeof(bound));
    if ((*env)->GetJavaVM(env, &actual) == JNI_OK && actual && bound == actual) {
        mask |= 2;
        if (((JNIEnv *(*)(void))getter)() == env) mask |= 4;
    }
    printf("ART_ENGINE_VM_SLOT_MATCH\t%u\n", (mask & 2) != 0);
    printf("ART_ENGINE_ENV_GETTER_MATCH\t%u\n", (mask & 4) != 0);
    fflush(stdout);
    return (jint)mask;
}
