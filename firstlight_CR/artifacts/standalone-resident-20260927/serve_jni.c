#include <jni.h>
#include <dlfcn.h>
#include <unistd.h>

JNIEXPORT jint JNICALL Java_AssetProbe_serve(JNIEnv *env, jclass cls,
                                           jstring library, jstring root, jint port) {
    (void)cls;
    const char *path = (*env)->GetStringUTFChars(env, library, NULL);
    if (!path) return 60;
    void *handle = dlopen(path, RTLD_NOW | RTLD_LOCAL);
    (*env)->ReleaseStringUTFChars(env, library, path);
    if (!handle) return 61;
    int (*serve)(JavaVM *, const char *, int) = dlsym(handle, "cr_standalone_serve");
    JavaVM *vm = NULL;
    if (!serve || (*env)->GetJavaVM(env, &vm) != JNI_OK) return 62;
    const char *directory = (*env)->GetStringUTFChars(env, root, NULL);
    if (!directory) return 63;
    alarm(0);
    const int status = serve(vm, directory, port);
    (*env)->ReleaseStringUTFChars(env, root, directory);
    return status;
}
