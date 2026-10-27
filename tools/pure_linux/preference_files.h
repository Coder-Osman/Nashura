#ifndef NASHURA_PREFERENCE_FILES_H
#define NASHURA_PREFERENCE_FILES_H

#include <stddef.h>
#include <stdint.h>

#define PREFERENCE_FILE_LIMIT 65536

enum PreferenceFileEvent {
    PF_RENAME_BACKUP, PF_OPEN_OUTPUT, PF_WRITE, PF_SYNC, PF_CLOSE_OUTPUT,
    PF_REMOVE_BACKUP, PF_RESTORE_BACKUP, PF_EVENT_COUNT
};

typedef struct PreferenceFiles PreferenceFiles;
typedef int (*PreferenceFileObserver)(void *, enum PreferenceFileEvent, unsigned int);
typedef struct {
    int committed;
    int error;
    int sync_error;
    int backup_remove_error;
} PreferenceFileResult;

int preference_files_open(const char *, PreferenceFileObserver, void *, PreferenceFiles **);
int preference_files_load(PreferenceFiles *, unsigned int, void *, size_t, size_t *, int *);
int preference_files_commit(PreferenceFiles *, unsigned int, const void *, size_t, PreferenceFileResult *);
int preference_files_close(PreferenceFiles *);

#endif
