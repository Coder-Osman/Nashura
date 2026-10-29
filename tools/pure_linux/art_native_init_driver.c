/* Compile native_init_probe.c with -Dmain=nashura_native_init_main, then link
   this JNI driver and the existing guarded provider probe into one shared object. */
#include <fcntl.h>
#include <jni.h>
#include <stdio.h>
#include <unistd.h>

extern int nashura_native_init_main(int, char **);

JNIEXPORT jint JNICALL Java_ArtNativeInitProbe_run(JNIEnv *env, jclass cls,
        jstring engine, jstring directory, jstring content_guard, jstring filesystem_guard) {
    (void)cls;
    static int invoked;
    if (invoked++) return 110;
    jstring strings[] = {engine, directory, content_guard, filesystem_guard};
    const char *values[4] = {0};
    int count = 0;
    for (; count < 4; ++count) {
        if (!strings[count]) break;
        values[count] = (*env)->GetStringUTFChars(env, strings[count], NULL);
        if (!values[count]) break;
    }
    jint result = 111;
    if (count == 4) {
        int descriptor = open(values[1], O_RDONLY | O_DIRECTORY | O_NOFOLLOW | O_CLOEXEC);
        if (descriptor >= 0) {
            if (fchdir(descriptor) == 0) {
                char *arguments[] = {"art-native-init", (char *)values[0],
                                     (char *)values[2], (char *)values[3], NULL};
                result = nashura_native_init_main(4, arguments);
            }
            close(descriptor);
        }
    }
    for (int i = 0; i < count; ++i) {
        (*env)->ReleaseStringUTFChars(env, strings[i], values[i]);
    }
    int pending = (*env)->ExceptionCheck(env) == JNI_TRUE;
    printf("ART_NATIVE_INIT_PENDING_EXCEPTION\t%d\n", pending);
    fflush(stdout);
    if (pending) return 112;
    return result;
}
