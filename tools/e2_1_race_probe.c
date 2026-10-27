#include "e2_1_concurrent_probe.c"

typedef struct {
    WorkerResult worker;
    int own_reads;
    int other_reads;
    int unknown_reads;
    int index;
} RaceResult;

static RaceResult races[2];
static pthread_barrier_t round_barrier;
static const char *race_values[2] = {"731", "947"};

static void *race_worker(void *argument) {
    RaceResult *result = argument;
    result->worker.thread = (int)syscall(SYS_gettid);
    pthread_mutex_lock(&launch_mutex);
    while (!launch) pthread_cond_wait(&launch_condition, &launch_mutex);
    int permitted = launch > 0;
    pthread_mutex_unlock(&launch_mutex);
    if (!permitted) { result->worker.error = 1; return NULL; }
    JNIEnv *environment = attach();
    JavaVM *vm = NULL;
    int usable = environment && (*environment)->GetJavaVM(environment, &vm) == JNI_OK && vm;
    if (!usable) result->worker.error = 2;
    for (int iteration = 0; iteration < 4; ++iteration) {
        pthread_barrier_wait(&round_barrier);
        if (usable && !result->worker.error) {
            if ((*environment)->ExceptionCheck(environment)) result->worker.error = 3;
            else {
                EngineString key = {0}, value = {0}, output = {0};
                construct(&key, "PN_POPUP");
                construct(&value, race_values[result->index]);
                write_value(&key, &value);
                if ((*environment)->ExceptionCheck(environment)) result->worker.error = 4;
                else {
                    e21_read(&key, &output, read_value);
                    if ((*environment)->ExceptionCheck(environment)) result->worker.error = 5;
                    else if (equals(&output, race_values[result->index])) ++result->own_reads;
                    else if (equals(&output, race_values[1 - result->index])) ++result->other_reads;
                    else ++result->unknown_reads;
                    destroy(&output);
                }
                destroy(&value);
                destroy(&key);
                if (!result->worker.error) ++result->worker.completed;
            }
        }
        pthread_barrier_wait(&round_barrier);
    }
    if (usable) {
        result->worker.detached = (*vm)->DetachCurrentThread(vm) == JNI_OK;
        if (!result->worker.detached && !result->worker.error) result->worker.error = 7;
    }
    return NULL;
}

int e21_race(void *constructor, void *destructor, void *reader, void *writer, void *attacher) {
    if (!constructor || !destructor || !reader || !writer || !attacher) return -1;
    construct = (Constructor)constructor;
    destroy = (Destructor)destructor;
    read_value = reader;
    write_value = (Writer)writer;
    attach = (Attach)attacher;
    memset(races, 0, sizeof(races));
    races[1].index = 1;
    if (pthread_barrier_init(&round_barrier, NULL, 2)) return -4;
    pthread_t threads[2];
    launch = 0;
    int created = 0;
    for (; created < 2; ++created) {
        if (pthread_create(&threads[created], NULL, race_worker, &races[created])) break;
    }
    pthread_mutex_lock(&launch_mutex);
    launch = created == 2 ? 1 : -1;
    pthread_cond_broadcast(&launch_condition);
    pthread_mutex_unlock(&launch_mutex);
    int joined = 1;
    for (int index = 0; index < created; ++index) {
        if (pthread_join(threads[index], NULL)) joined = 0;
    }
    if (!joined) return -3;
    pthread_barrier_destroy(&round_barrier);
    if (created != 2) return -2;
    return races[0].worker.error || races[1].worker.error;
}

const RaceResult *e21_race_result(int index) {
    return index >= 0 && index < 2 ? &races[index] : NULL;
}
