/* Verify a real VM, Java callback and native-thread attachment lifecycle. */
#include <jni.h>
#include <pthread.h>
#include <stdio.h>

static JavaVM *runtime;

struct thread_check {
    jclass callback_class;
    jmethodID callback;
    unsigned mask;
};

JNIEXPORT jint JNI_OnLoad(JavaVM *vm, void *reserved) {
    (void)reserved;
    JNIEnv *env = NULL;
    if ((*vm)->GetEnv(vm, (void **)&env, JNI_VERSION_1_6) != JNI_OK || !env) {
        return JNI_ERR;
    }
    runtime = vm;
    return JNI_VERSION_1_6;
}

static void *worker(void *opaque) {
    struct thread_check *check = opaque;
    JNIEnv *env = NULL;
    if ((*runtime)->GetEnv(runtime, (void **)&env, JNI_VERSION_1_6) == JNI_EDETACHED) {
        check->mask |= 4;
    }
    if ((*runtime)->AttachCurrentThread(runtime, &env, NULL) != JNI_OK || !env) {
        return NULL;
    }
    check->mask |= 8;
    JavaVM *observed = NULL;
    if ((*env)->GetJavaVM(env, &observed) == JNI_OK && observed == runtime) {
        jlong result = (*env)->CallStaticLongMethod(env, check->callback_class,
                                                  check->callback, (jlong)49995000);
        if (!(*env)->ExceptionCheck(env) && result == 49995042) {
            check->mask |= 16;
        }
    }
    if ((*env)->ExceptionCheck(env)) {
        (*env)->ExceptionDescribe(env);
        (*env)->ExceptionClear(env);
    }
    if ((*runtime)->DetachCurrentThread(runtime) == JNI_OK) {
        check->mask |= 32;
    }
    env = NULL;
    if ((*runtime)->GetEnv(runtime, (void **)&env, JNI_VERSION_1_6) == JNI_EDETACHED) {
        check->mask |= 64;
    }
    return NULL;
}

JNIEXPORT jint JNICALL Java_ArtRuntimeProbe_exercise(JNIEnv *env, jclass cls) {
    JavaVM *observed = NULL;
    unsigned mask = 0;
    if (runtime && (*env)->GetJavaVM(env, &observed) == JNI_OK && observed == runtime) {
        mask |= 1;
    } else {
        return 0;
    }
    JNIEnv *current = NULL;
    if ((*runtime)->GetEnv(runtime, (void **)&current, JNI_VERSION_1_6) == JNI_OK
            && current == env) {
        mask |= 2;
    }
    struct thread_check check = {0};
    check.callback_class = (*env)->NewGlobalRef(env, cls);
    check.callback = (*env)->GetStaticMethodID(env, cls, "callback", "(J)J");
    if (!check.callback_class || !check.callback || (*env)->ExceptionCheck(env)) {
        if (check.callback_class) (*env)->DeleteGlobalRef(env, check.callback_class);
        return (jint)mask;
    }
    pthread_t thread;
    if (pthread_create(&thread, NULL, worker, &check) != 0) {
        (*env)->DeleteGlobalRef(env, check.callback_class);
        return (jint)mask;
    }
    if (pthread_join(thread, NULL) != 0) {
        /* Do not release an object that a running thread may still access. */
        return (jint)mask;
    }
    (*env)->DeleteGlobalRef(env, check.callback_class);
    mask |= check.mask;
    printf("ART_JNI_LIFECYCLE\t%u\n", mask);
    return (jint)mask;
}
