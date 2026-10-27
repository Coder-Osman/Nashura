#define _GNU_SOURCE
#include "preference_files.h"
#include "mapped_files.h"

#include <errno.h>
#include <fcntl.h>
#include <limits.h>
#include <pthread.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/types.h>
#include <sys/wait.h>
#include <unistd.h>

static const char original[] = "<map><string name=\"synthetic\">old</string></map>";
static const char changed[] = "<map><string name=\"synthetic\">new</string></map>";
static unsigned int assertions;
static const char *executable, *linker;

#define CHECK(condition) do { assertions++; if (!(condition)) { \
    fprintf(stderr, "assertion_failed:%d\n", __LINE__); exit(1); } } while (0)

typedef struct {
    int failure, crash;
    unsigned int calls[PF_EVENT_COUNT];
} Observer;

static int observe(void *context, enum PreferenceFileEvent event, unsigned int slot) {
    Observer *observer = context;
    CHECK(slot < 2 && event < PF_EVENT_COUNT);
    observer->calls[event]++;
    if ((int)event == observer->crash) _exit(73);
    return (int)event == observer->failure ? EIO : 0;
}

static void directory_path(char *output, size_t capacity, const char *base, const char *name) {
    int length = snprintf(output, capacity, "%s/%s", base, name);
    CHECK(length > 0 && (size_t)length < capacity);
}

static void create_case(char *output, const char *base, const char *name) {
    directory_path(output, PATH_MAX, base, name);
    CHECK(!mkdir(output, 0700));
}

static PreferenceFiles *open_files(const char *directory, Observer *observer) {
    PreferenceFiles *files = NULL;
    CHECK(!preference_files_open(directory, observer ? observe : NULL, observer, &files));
    CHECK(files != NULL);
    return files;
}

static void commit_bytes(PreferenceFiles *files, unsigned int slot, const char *bytes) {
    PreferenceFileResult result;
    CHECK(!preference_files_commit(files, slot, bytes, strlen(bytes), &result));
    CHECK(result.committed && !result.error && !result.sync_error && !result.backup_remove_error);
}

static void expect_bytes(PreferenceFiles *files, unsigned int slot, const char *expected) {
    unsigned char buffer[256];
    memset(buffer, 0xA5, sizeof(buffer));
    size_t length = 999;
    int present = -1;
    CHECK(!preference_files_load(files, slot, buffer + 1, sizeof(buffer) - 2, &length, &present));
    CHECK(present == (expected != NULL));
    CHECK(length == (expected ? strlen(expected) : 0));
    if (expected) CHECK(!memcmp(buffer + 1, expected, length));
    CHECK(buffer[0] == 0xA5 && buffer[length + 1] == 0xA5 && buffer[255] == 0xA5);
}

static void expect_file(const char *directory, const char *name, const char *expected) {
    char path[PATH_MAX], bytes[256];
    directory_path(path, sizeof(path), directory, name);
    int descriptor = open(path, O_RDONLY);
    if (!expected) { CHECK(descriptor < 0 && errno == ENOENT); return; }
    CHECK(descriptor >= 0);
    ssize_t length = read(descriptor, bytes, sizeof(bytes));
    CHECK(length == (ssize_t)strlen(expected) && !memcmp(bytes, expected, (size_t)length));
    CHECK(!close(descriptor));
}

static int child(const char *mode, const char *directory, const char *value) {
    pid_t process = fork();
    CHECK(process >= 0);
    if (!process) {
        if (strcmp(linker, "-")) execl(linker, linker, executable, mode, directory, value, NULL);
        else execl(executable, executable, mode, directory, value, NULL);
        _exit(111);
    }
    int status;
    CHECK(waitpid(process, &status, 0) == process && WIFEXITED(status));
    return WEXITSTATUS(status);
}

static void report_case(const char *name, unsigned int *cases) {
    printf("%s\"%s\"", (*cases)++ ? "," : "", name);
}

static void *foreign_thread(void *context) {
    PreferenceFiles *files = context;
    unsigned char buffer[256];
    size_t length;
    int present;
    PreferenceFileResult result;
    int rejected = preference_files_load(files, 0, buffer, sizeof(buffer), &length, &present) == -1 &&
        preference_files_commit(files, 0, changed, strlen(changed), &result) == -1 &&
        preference_files_close(files) == -1;
    return (void *)(uintptr_t)rejected;
}

static void restrictions(const char *base, unsigned int *cases) {
    char directory[PATH_MAX], path[PATH_MAX], target[PATH_MAX];
    create_case(directory, base, "restrictions");
    PreferenceFiles *files = open_files(directory, NULL), *other = NULL;
    CHECK(preference_files_open(directory, NULL, NULL, &other) == -1 && other == NULL);
    commit_bytes(files, 0, original);
    pthread_t thread;
    void *thread_result;
    CHECK(!pthread_create(&thread, NULL, foreign_thread, files));
    CHECK(!pthread_join(thread, &thread_result) && (uintptr_t)thread_result == 1);
    pid_t process = fork();
    CHECK(process >= 0);
    if (!process) _exit((uintptr_t)foreign_thread(files) == 1 ? 0 : 1);
    int status;
    CHECK(waitpid(process, &status, 0) == process && WIFEXITED(status) && !WEXITSTATUS(status));
    expect_bytes(files, 0, original);
    CHECK(!preference_files_close(files));
    report_case("lock_and_owner", cases);
    directory_path(path, sizeof(path), directory, "store-1.xml");
    directory_path(target, sizeof(target), directory, "store-0.xml");
    for (unsigned int kind = 0; kind < 4; kind++) {
        if (kind == 0) CHECK(!symlink(target, path));
        if (kind == 1) CHECK(!link(target, path));
        if (kind == 2) CHECK(!mkdir(path, 0700));
        if (kind == 3) {
            int descriptor = open(path, O_CREAT | O_WRONLY | O_EXCL, 0600);
            CHECK(descriptor >= 0 && !ftruncate(descriptor, PREFERENCE_FILE_LIMIT + 1));
            CHECK(!close(descriptor));
        }
        files = open_files(directory, NULL);
        char bytes[256];
        size_t length;
        int present;
        PreferenceFileResult result;
        CHECK(preference_files_load(files, 1, bytes, sizeof(bytes), &length, &present) == -1);
        CHECK(preference_files_commit(files, 1, changed, strlen(changed), &result) == -1);
        CHECK(!preference_files_close(files));
        CHECK(!(kind == 2 ? rmdir(path) : unlink(path)));
    }
    report_case("nonregular_and_oversize_rejected", cases);
    char linked_directory[PATH_MAX];
    directory_path(linked_directory, sizeof(linked_directory), base, "linked-directory");
    CHECK(!symlink(directory, linked_directory));
    CHECK(preference_files_open(linked_directory, NULL, NULL, &other) == -1 && other == NULL);
    CHECK(preference_files_open("relative", NULL, NULL, &other) == -1 && other == NULL);
    report_case("directory_and_path_rejected", cases);
}

int main(int count, char **arguments) {
    if (count == 4 && !strcmp(arguments[1], "--verify")) {
        PreferenceFiles *files = open_files(arguments[2], NULL);
        expect_bytes(files, 0, !strcmp(arguments[3], "old") ? original : changed);
        CHECK(!preference_files_close(files));
        return 0;
    }
    if (count == 4 && !strcmp(arguments[1], "--crash")) {
        Observer observer = {.failure = -1, .crash = atoi(arguments[3])};
        PreferenceFiles *files = open_files(arguments[2], &observer);
        commit_bytes(files, 0, changed);
        _exit(74);
    }
    if (count != 3 || arguments[1][0] != '/') return 2;
    executable = arguments[0];
    linker = arguments[2];
    CHECK(!mkdir(arguments[1], 0700));
    observe_mapped_files();
    unsigned int cases = 0;
    printf("{\"scope\":\"synthetic_file_commit_and_process_crash_only\",\"libg_loaded\":false,\"cases\":[");
    char directory[PATH_MAX];
    create_case(directory, arguments[1], "normal");
    PreferenceFiles *files = open_files(directory, NULL);
    expect_bytes(files, 0, NULL);
    expect_bytes(files, 1, NULL);
    commit_bytes(files, 0, original);
    commit_bytes(files, 1, changed);
    expect_bytes(files, 0, original);
    expect_bytes(files, 1, changed);
    CHECK(!preference_files_close(files));
    char normal_path[PATH_MAX];
    struct stat normal_status;
    directory_path(normal_path, sizeof(normal_path), directory, "store-0.xml");
    CHECK(!stat(normal_path, &normal_status) && (normal_status.st_mode & 0777) == 0660);
    CHECK(child("--verify", directory, "old") == 0);
    files = open_files(directory, NULL);
    expect_bytes(files, 1, changed);
    CHECK(!preference_files_close(files));
    report_case("two_slots_and_exec_restart", &cases);
    for (int stage = PF_RENAME_BACKUP; stage <= PF_REMOVE_BACKUP; stage++) {
        char name[64];
        snprintf(name, sizeof(name), "failure-%d", stage);
        create_case(directory, arguments[1], name);
        Observer observer = {.failure = -1, .crash = -1};
        files = open_files(directory, &observer);
        commit_bytes(files, 0, original);
        memset(observer.calls, 0, sizeof(observer.calls));
        observer.failure = stage;
        PreferenceFileResult result;
        CHECK(!preference_files_commit(files, 0, changed, strlen(changed), &result));
        CHECK(observer.calls[stage] == 1);
        if (stage == PF_SYNC) {
            CHECK(result.committed && !result.error && result.sync_error == EIO && !result.backup_remove_error);
            expect_file(directory, "store-0.xml", changed);
            expect_file(directory, "store-0.xml.bak", NULL);
        } else if (stage == PF_REMOVE_BACKUP) {
            CHECK(result.committed && !result.error && !result.sync_error && result.backup_remove_error == EIO);
            expect_file(directory, "store-0.xml", changed);
            expect_file(directory, "store-0.xml.bak", original);
        } else CHECK(!result.committed && result.error == EIO && !result.sync_error && !result.backup_remove_error);
        observer.failure = -1;
        expect_bytes(files, 0, stage == PF_SYNC ? changed : original);
        CHECK(!preference_files_close(files));
        CHECK(child("--verify", directory, stage == PF_SYNC ? "new" : "old") == 0);
        report_case(name, &cases);
    }
    for (int stage = PF_OPEN_OUTPUT; stage <= PF_REMOVE_BACKUP; stage++) {
        char name[64], value[32];
        snprintf(name, sizeof(name), "crash-before-%d", stage);
        snprintf(value, sizeof(value), "%d", stage);
        create_case(directory, arguments[1], name);
        files = open_files(directory, NULL);
        commit_bytes(files, 0, original);
        CHECK(!preference_files_close(files));
        CHECK(child("--crash", directory, value) == 73);
        CHECK(child("--verify", directory, "old") == 0);
        expect_file(directory, "store-0.xml.bak", NULL);
        report_case(name, &cases);
    }
    create_case(directory, arguments[1], "completed-crash");
    files = open_files(directory, NULL);
    commit_bytes(files, 0, original);
    CHECK(!preference_files_close(files));
    CHECK(child("--crash", directory, "-1") == 74);
    CHECK(child("--verify", directory, "new") == 0);
    report_case("crash_after_commit", &cases);
    create_case(directory, arguments[1], "restore-failure");
    Observer observer = {.failure = -1, .crash = -1};
    files = open_files(directory, &observer);
    commit_bytes(files, 0, original);
    observer.failure = PF_WRITE;
    PreferenceFileResult result;
    CHECK(!preference_files_commit(files, 0, changed, strlen(changed), &result) && !result.committed);
    observer.failure = PF_RESTORE_BACKUP;
    unsigned char bytes[256];
    size_t length;
    int present;
    CHECK(preference_files_load(files, 0, bytes, sizeof(bytes), &length, &present) == -1);
    expect_file(directory, "store-0.xml.bak", original);
    observer.failure = -1;
    expect_bytes(files, 0, original);
    CHECK(!preference_files_close(files));
    report_case("restore_failure_preserves_backup", &cases);
    create_case(directory, arguments[1], "existing-backup");
    files = open_files(directory, &observer);
    commit_bytes(files, 0, original);
    observer.failure = PF_REMOVE_BACKUP;
    CHECK(!preference_files_commit(files, 0, changed, strlen(changed), &result) && result.committed);
    expect_file(directory, "store-0.xml.bak", original);
    observer.failure = PF_WRITE;
    CHECK(!preference_files_commit(files, 0, changed, strlen(changed), &result) && !result.committed);
    expect_file(directory, "store-0.xml", NULL);
    expect_file(directory, "store-0.xml.bak", original);
    observer.failure = -1;
    expect_bytes(files, 0, original);
    CHECK(!preference_files_close(files));
    report_case("existing_backup_wins_after_repeated_write", &cases);
    create_case(directory, arguments[1], "bounds");
    files = open_files(directory, NULL);
    unsigned char *full = malloc(PREFERENCE_FILE_LIMIT + 2);
    CHECK(full != NULL);
    memset(full, 'X', PREFERENCE_FILE_LIMIT + 2);
    full[0] = 0xA5;
    full[PREFERENCE_FILE_LIMIT + 1] = 0x5A;
    CHECK(!preference_files_commit(files, 0, full + 1, PREFERENCE_FILE_LIMIT, &result) && result.committed);
    memset(full + 1, 0xCC, PREFERENCE_FILE_LIMIT);
    CHECK(preference_files_load(files, 0, full + 1, PREFERENCE_FILE_LIMIT - 1, &length, &present) == -1);
    CHECK(!length && !present && full[1] == 0xCC);
    CHECK(!preference_files_load(files, 0, full + 1, PREFERENCE_FILE_LIMIT, &length, &present));
    CHECK(present && length == PREFERENCE_FILE_LIMIT && full[0] == 0xA5 && full[PREFERENCE_FILE_LIMIT + 1] == 0x5A);
    int matched = 1;
    for (size_t offset = 0; offset < PREFERENCE_FILE_LIMIT; offset++) matched &= full[offset + 1] == 'X';
    CHECK(matched);
    CHECK(preference_files_commit(files, 0, full, PREFERENCE_FILE_LIMIT + 1, &result) == -1);
    CHECK(preference_files_commit(files, 0, "", 0, &result) == -1);
    CHECK(preference_files_commit(files, 2, changed, strlen(changed), &result) == -1);
    free(full);
    CHECK(!preference_files_close(files));
    report_case("capacity_and_output_guard", &cases);
    restrictions(arguments[1], &cases);
    printf("],\"case_count\":%u,\"assertions\":%u,\"status\":\"PASS\"}\n", cases, assertions);
    return 0;
}
