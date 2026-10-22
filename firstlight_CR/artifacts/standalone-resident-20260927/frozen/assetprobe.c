// Register original Android natives in a real VM; no replacement JNI methods.
#define _GNU_SOURCE
#include <jni.h>
#include <errno.h>
#include <dlfcn.h>
#include <stdio.h>
#include <stdint.h>
#include <string.h>
#include <unistd.h>
#include <fcntl.h>
#include <sys/stat.h>

extern int resource_probe_run(int argc, char **argv);

JNIEXPORT void JNICALL Java_AssetProbe_inspectProperties(JNIEnv *env, jclass cls) {
    (void)env; (void)cls;
    int (*initialize)(void) = dlsym(RTLD_DEFAULT, "__system_properties_init");
    errno = 0;
    int initialized = initialize ? initialize() : -1;
    int init_errno = errno;
    int (*property_get)(const char *, char *) = dlsym(RTLD_DEFAULT, "__system_property_get");
    char abi[128] = {0};
    int abi_length = property_get ? property_get("ro.product.cpu.abilist64", abi) : -1;
    printf("{\"event\":\"original_system_properties_reinitialized\",\"available\":%s,"
           "\"status\":%d,\"errno\":%d,\"abi_length\":%d,\"abi\":\"%s\"}\n",
           initialize ? "true" : "false", initialized, init_errno, abi_length, abi);
    const char *path = "/dev/__properties__/property_info";
    int fd = open(path, O_RDONLY);
    struct stat st = {0};
    unsigned char header[16] = {0};
    ssize_t count = fd >= 0 ? read(fd, header, sizeof(header)) : -1;
    if (fd >= 0) fstat(fd, &st);
    if (fd >= 0) close(fd);
    printf("{\"event\":\"original_property_area_file\",\"bytes\":%lld,"
           "\"read\":%ld,\"header\":\"", (long long)st.st_size, (long)count);
    for (unsigned i = 0; i < sizeof(header); ++i) printf("%02x", header[i]);
    puts("\"}");
}

JNIEXPORT jint JNICALL Java_AssetProbe_bootstrapGameAssets(JNIEnv *env, jclass cls,
                                                         jobject manager, jstring root) {
    (void)cls;
    const char *directory = (*env)->GetStringUTFChars(env, root, NULL);
    if (!directory) return 20;
    char library[4096], scdb[4096];
    int n = snprintf(library, sizeof(library), "%s/app-libs/libg.so", directory);
    int m = snprintf(scdb, sizeof(scdb), "%s/resources/runtime-update/assets.scdb", directory);
    (*env)->ReleaseStringUTFChars(env, root, directory);
    if (n < 0 || n >= (int)sizeof(library) || m < 0 || m >= (int)sizeof(scdb)) return 21;
    puts("{\"event\":\"before_libandroid_load\"}"); fflush(stdout);
    void *android = dlopen("libandroid.so", RTLD_NOW | RTLD_LOCAL);
    void *(*from_java)(JNIEnv *, jobject) = android ? dlsym(android, "AAssetManager_fromJava") : NULL;
    printf("{\"event\":\"libandroid_loaded\",\"library\":%s,\"converter\":%s}\n",
           android ? "true" : "false", from_java ? "true" : "false"); fflush(stdout);
    void *runtime = dlopen("libandroid_runtime.so", RTLD_NOW | RTLD_NOLOAD);
    void **offset = runtime ? dlsym(runtime, "_ZN7android20gAssetManagerOffsetsE") : NULL;
    void **android_offset = android ? dlsym(android, "_ZN7android20gAssetManagerOffsetsE") : NULL;
    Dl_info offset_info = {0};
    Dl_info android_offset_info = {0};
    if (offset) dladdr(offset, &offset_info);
    if (android_offset) dladdr(android_offset, &android_offset_info);
    printf("{\"event\":\"asset_manager_field_registration\",\"runtime\":%s,"
           "\"field_id\":\"%p\",\"owner\":\"%s\","
           "\"android_slot\":\"%p\",\"android_field_id\":\"%p\","
           "\"android_owner\":\"%s\",\"same_slot\":%s}\n",
           runtime ? "true" : "false", offset ? *offset : NULL,
           offset_info.dli_fname ? offset_info.dli_fname : "", android_offset,
           android_offset ? *android_offset : NULL,
           android_offset_info.dli_fname ? android_offset_info.dli_fname : "",
           android_offset == offset ? "true" : "false"); fflush(stdout);
    jclass manager_class = (*env)->GetObjectClass(env, manager);
    jfieldID object_field = manager_class ? (*env)->GetFieldID(env, manager_class, "mObject", "J") : NULL;
    printf("{\"event\":\"asset_manager_instance_field\",\"field_id\":\"%p\","
           "\"matches_registration\":%s,\"exception\":%s}\n",
           object_field, offset && object_field == *offset ? "true" : "false",
           (*env)->ExceptionCheck(env) ? "true" : "false"); fflush(stdout);
    if (!object_field || (*env)->ExceptionCheck(env)) return 24;
    puts("{\"event\":\"before_asset_manager_native_field_read\"}"); fflush(stdout);
    jlong native_object = (*env)->GetLongField(env, manager, object_field);
    printf("{\"event\":\"asset_manager_native_field_read\",\"present\":%s}\n",
           native_object ? "true" : "false"); fflush(stdout);
    puts("{\"event\":\"before_asset_manager_from_java\"}"); fflush(stdout);
    void *assets = from_java ? from_java(env, manager) : NULL;
    printf("{\"event\":\"asset_manager_from_java_returned\",\"present\":%s}\n",
           assets ? "true" : "false"); fflush(stdout);
    if (!assets || (*env)->ExceptionCheck(env)) return 22;
    puts("{\"event\":\"before_libg_load\"}"); fflush(stdout);
    void *g = dlopen(library, RTLD_NOW | RTLD_LOCAL);
    printf("{\"event\":\"libg_load_returned\",\"present\":%s}\n", g ? "true" : "false"); fflush(stdout);
    Dl_info info;
    if (!g || !dladdr(dlsym(g, "JNI_OnLoad"), &info)) return 23;
    JavaVM *vm = NULL, *original_vm = NULL;
    if ((*env)->GetJavaVM(env, &vm) != JNI_OK) return 25;
    memcpy(&original_vm, (char *)info.dli_fbase + 0x01a16b78, sizeof(original_vm));
    printf("{\"event\":\"original_jni_vm_identity\",\"matches\":%s}\n",
           vm && original_vm == vm ? "true" : "false"); fflush(stdout);
    if (!vm || original_vm != vm) return 25;
    // Same original setter called by createGameMain at 0x13d0348, fed a real
    // AAssetManager_fromJava result whose Java lifetime spans all native work.
    puts("{\"event\":\"before_game_asset_manager_bind\"}"); fflush(stdout);
    ((void (*)(void *))((uintptr_t)info.dli_fbase + 0x01195a88))(assets);
    puts("{\"event\":\"real_asset_manager_bound_to_game\"}");
    void *(*asset_open)(void *, const char *, int) = dlsym(android, "AAssetManager_open");
    void (*asset_close)(void *) = dlsym(android, "AAsset_close");
    const char *asset_names[] = {"csv_logic/globals.csv", "csv_logic/global_ids_oldformat.csv"};
    for (unsigned i = 0; i < sizeof(asset_names) / sizeof(asset_names[0]); i++) {
        void *asset = asset_open ? asset_open(assets, asset_names[i], 2) : NULL;
        printf("{\"event\":\"original_apk_native_asset_lookup\",\"name\":\"%s\",\"present\":%s}\n",
               asset_names[i], asset ? "true" : "false");
        if (asset && asset_close) asset_close(asset);
    }
    char *argv[] = {"resource_probe", library, scdb, "--apk-reader-list"};
    int status = resource_probe_run(4, argv);
    alarm(0);
    return status;
}

JNIEXPORT void JNICALL Java_AssetProbe_registerFramework(JNIEnv *env, jclass cls) {
    (void)cls;
    // Resolve registration through libandroid's dependency group so its
    // AAssetManager_fromJava reads the same runtime's cached field ID.
    void *h = dlopen("libandroid.so", RTLD_NOW | RTLD_GLOBAL);
    if (!h) {
        (*env)->ThrowNew(env, (*env)->FindClass(env, "java/lang/UnsatisfiedLinkError"), dlerror());
        return;
    }
    const char *names[] = {
        "_ZN7android36register_android_os_SystemPropertiesEP7_JNIEnv",
        "_ZN7android25register_android_util_LogEP7_JNIEnv",
        "_ZN7android25register_android_os_TraceEP7_JNIEnv",
        "_ZN7android31register_android_os_SystemClockEP7_JNIEnv",
        "_ZN7android32register_android_os_MessageQueueEP7_JNIEnv",
        "_Z27register_android_os_ProcessP7_JNIEnv",
        "_ZN7android26register_android_os_ParcelEP7_JNIEnv",
        "_Z26register_android_os_BinderP7_JNIEnv",
        "_ZN7android38register_android_content_res_ApkAssetsEP7_JNIEnv",
        "_ZN7android36register_android_content_StringBlockEP7_JNIEnv",
        "_ZN7android33register_android_content_XmlBlockEP7_JNIEnv",
        "_ZN7android37register_android_content_AssetManagerEP7_JNIEnv"
    };
    for (unsigned i = 0; i < sizeof(names)/sizeof(names[0]); i++) {
        int (*fn)(JNIEnv *) = dlsym(h, names[i]);
        if (!fn || fn(env) < 0 || (*env)->ExceptionCheck(env)) {
            if (!(*env)->ExceptionCheck(env))
                (*env)->ThrowNew(env, (*env)->FindClass(env, "java/lang/UnsatisfiedLinkError"), names[i]);
            return;
        }
        printf("{\"event\":\"original_framework_native_registered\",\"index\":%u}\n", i);
        fflush(stdout);
    }
}
