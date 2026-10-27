#include <jni.h>
#include <pthread.h>
#include <stdint.h>
#include <string.h>
#include <sys/syscall.h>
#include <unistd.h>

typedef struct { uint64_t words[2]; } EngineString;
typedef void (*Constructor)(EngineString *, const char *);
typedef void (*Destructor)(EngineString *);
typedef void (*Writer)(EngineString *, EngineString *);
typedef JNIEnv *(*Attach)(void);

extern void e21_read(EngineString *, EngineString *, void *);
__asm__(".text\n.p2align 2\n.type e21_read,%function\ne21_read:\n"
        "stp x29,x30,[sp,#-16]!\nmov x8,x1\nblr x2\n"
        "ldp x29,x30,[sp],#16\nret\n.size e21_read,.-e21_read\n");

typedef struct {
    int thread;
    int completed;
    int error;
    int detached;
} WorkerResult;

static Constructor construct;
static Destructor destroy;
static Writer write_value;
static Attach attach;
static void *read_value;
static const char *key_text;
static const char *value_text;
static pthread_mutex_t launch_mutex = PTHREAD_MUTEX_INITIALIZER;
static pthread_cond_t launch_condition = PTHREAD_COND_INITIALIZER;
static int launch;
static WorkerResult results[2];

static int equals(const EngineString *value, const char *expected) {
    uint32_t length;
    memcpy(&length, (const char *)value + 4, sizeof(length));
    if (length != strlen(expected)) return 0;
    uintptr_t address;
    if (length < 8) address = (uintptr_t)value + 8;
    else {
        memcpy(&address, (const char *)value + 8, sizeof(address));
        address &= UINT64_C(0x00ffffffffffffff);
    }
    return address && memcmp((const void *)address, expected, length + 1) == 0;
}

static void *worker(void *argument) {
    WorkerResult *result = argument;
    result->thread = (int)syscall(SYS_gettid);
    pthread_mutex_lock(&launch_mutex);
    while (!launch) pthread_cond_wait(&launch_condition, &launch_mutex);
    int permitted = launch > 0;
    pthread_mutex_unlock(&launch_mutex);
    if (!permitted) { result->error = 1; return NULL; }
    JNIEnv *environment = attach();
    JavaVM *vm = NULL;
    if (!environment || (*environment)->GetJavaVM(environment, &vm) != JNI_OK || !vm) {
        result->error = 2;
        return NULL;
    }
    for (int iteration = 0; iteration < 4; ++iteration) {
        if ((*environment)->ExceptionCheck(environment)) { result->error = 3; break; }
        EngineString key = {0}, value = {0}, output = {0};
        construct(&key, key_text);
        construct(&value, value_text);
        write_value(&key, &value);
        if ((*environment)->ExceptionCheck(environment)) result->error = 4;
        else {
            e21_read(&key, &output, read_value);
            if ((*environment)->ExceptionCheck(environment)) result->error = 5;
            else if (!equals(&output, value_text)) result->error = 6;
            destroy(&output);
        }
        destroy(&value);
        destroy(&key);
        if (result->error) break;
        ++result->completed;
    }
    result->detached = (*vm)->DetachCurrentThread(vm) == JNI_OK;
    if (!result->detached && !result->error) result->error = 7;
    return NULL;
}

int e21_concurrent(void *constructor, void *destructor, void *reader, void *writer,
                   void *attacher, const char *key, const char *value) {
    if (!constructor || !destructor || !reader || !writer || !attacher || !key || !value
            || strlen(key) > 4095 || strlen(value) > 4095) return -1;
    construct = (Constructor)constructor;
    destroy = (Destructor)destructor;
    read_value = reader;
    write_value = (Writer)writer;
    attach = (Attach)attacher;
    key_text = key;
    value_text = value;
    memset(results, 0, sizeof(results));
    launch = 0;
    pthread_t threads[2];
    int created = 0;
    for (; created < 2; ++created) {
        if (pthread_create(&threads[created], NULL, worker, &results[created])) break;
    }
    pthread_mutex_lock(&launch_mutex);
    launch = created == 2 ? 1 : -1;
    pthread_cond_broadcast(&launch_condition);
    pthread_mutex_unlock(&launch_mutex);
    for (int index = 0; index < created; ++index) {
        if (pthread_join(threads[index], NULL)) return -3;
    }
    if (created != 2) return -2;
    return results[0].error || results[1].error;
}

const WorkerResult *e21_worker_result(int index) {
    return index >= 0 && index < 2 ? &results[index] : NULL;
}
