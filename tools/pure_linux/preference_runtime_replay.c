#define _GNU_SOURCE
#include "preference_codec.h"
#include "preference_store.h"

#include <fcntl.h>
#include <stdio.h>
#include <string.h>
#include <unistd.h>

static unsigned int backup_calls, exception_calls;

static int unresolved_backup(void *context) {
    (void)context;
    backup_calls++;
    return -1;
}

static int unresolved_exception(void *context, unsigned int slot) {
    (void)context;
    (void)slot;
    exception_calls++;
    return -1;
}

static int capture(const char *directory, const char *stage, unsigned int slot) {
    char source[4096], destination[4096], bytes[PREFERENCE_FILE_LIMIT];
    int length = snprintf(source, sizeof(source), "%s/store-%u.xml", directory, slot);
    if (length < 0 || (size_t)length >= sizeof(source)) return -1;
    length = snprintf(destination, sizeof(destination), "%s/%s-%u.xml", directory, stage, slot);
    if (length < 0 || (size_t)length >= sizeof(destination)) return -1;
    int input = open(source, O_RDONLY | O_CLOEXEC | O_NOFOLLOW);
    if (input < 0) return -1;
    ssize_t size = read(input, bytes, sizeof(bytes));
    char extra;
    int valid = size >= 0 && read(input, &extra, 1) == 0;
    if (close(input)) valid = 0;
    if (!valid) return -1;
    int output = open(destination, O_WRONLY | O_CREAT | O_EXCL | O_CLOEXEC | O_NOFOLLOW, 0600);
    if (output < 0) return -1;
    valid = write(output, bytes, (size_t)size) == size && !fsync(output);
    if (close(output)) valid = 0;
    return valid ? 0 : -1;
}

int main(int argc, char **argv) {
    if (argc != 5 || !argv[4][0]) return 64;
    PreferenceCodecs *codecs = NULL;
    PreferenceStore *store = NULL;
    PreferenceBackend backend;
    PreferenceStoreSummary summary = {0};
    const char *key = "LOADING_SCREEN_STATE_LABEL";
    char output[PREFERENCE_CAPACITY];
    int valid = !preference_codecs_open_material(argv[1], argv[2], &codecs);
    PreferenceStoreOptions options = {codecs, preference_codecs_transform, unresolved_exception,
                                     unresolved_backup, NULL, NULL};
    valid = valid && !preference_store_open(argv[3], &options, &store) &&
        !preference_store_backend(store, &backend) &&
        !preference_store_summary(store, &summary) && summary.entries[0] == 0 && summary.entries[1] == 0 &&
        !preference_contract_read(&backend, key, output, sizeof(output)) && !output[0];
    int target_result = valid ? preference_contract_write(&backend, key, argv[4]) : -2;
    valid = valid && target_result == -1 && backup_calls == 1 && exception_calls == 0 &&
        !preference_store_summary(store, &summary) && summary.commits == 2 &&
        summary.disk_writes == 2 && summary.backup_failures == 1 &&
        summary.entries[0] == 1 && summary.entries[1] == 1 &&
        !capture(argv[3], "target", 0) && !capture(argv[3], "target", 1);
    if (store) { if (preference_store_close(store)) valid = 0; store = NULL; }
    valid = valid && !preference_store_open(argv[3], &options, &store) &&
        !preference_store_backend(store, &backend) &&
        !preference_contract_read(&backend, key, output, sizeof(output)) && !strcmp(output, argv[4]);
    int restore_result = valid ? preference_contract_write(&backend, key, "") : -2;
    valid = valid && restore_result == -1 && backup_calls == 2 && exception_calls == 0 &&
        !preference_store_summary(store, &summary) && summary.commits == 2 &&
        summary.disk_writes == 2 && summary.backup_failures == 1 &&
        summary.entries[0] == 0 && summary.entries[1] == 0 &&
        !preference_contract_read(&backend, key, output, sizeof(output)) && !output[0] &&
        !capture(argv[3], "restore", 0) && !capture(argv[3], "restore", 1);
    if (store && preference_store_close(store)) valid = 0;
    preference_codecs_close(codecs);
    printf("{\"status\":\"%s\",\"target_result\":%d,\"restore_result\":%d,"
           "\"backup_requests\":%u,\"decode_exceptions\":%u,\"backup_service\":\"UNRESOLVED\","
           "\"scope\":\"local empty-map replay only; external byte comparison required\","
           "\"libg_loaded\":false,\"catalog_ready_pass\":false}\n",
           valid ? "LOCAL_REPLAY_COMPLETE" : "BLOCKED", target_result, restore_result,
           backup_calls, exception_calls);
    return valid ? 0 : 2;
}
