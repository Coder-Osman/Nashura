#ifndef NASHURA_PREFERENCE_STORE_H
#define NASHURA_PREFERENCE_STORE_H

#include "preference_contract.h"
#include "preference_files.h"

#define PREFERENCE_MAP_LIMIT 128

typedef struct PreferenceStore PreferenceStore;
typedef struct {
    void *context;
    int (*codec)(void *, unsigned int, enum PreferenceCodec, const char *, char *, size_t);
    int (*decode_exception)(void *, unsigned int);
    int (*request_backup)(void *);
    PreferenceFileObserver file_observer;
    void *file_context;
} PreferenceStoreOptions;

typedef struct {
    uint64_t commits, disk_writes, false_commits, backup_requests, backup_failures;
    uint64_t decode_exceptions;
    size_t entries[2];
    PreferenceFileResult last_file_result;
} PreferenceStoreSummary;

int preference_store_open(const char *, const PreferenceStoreOptions *, PreferenceStore **);
int preference_store_backend(PreferenceStore *, PreferenceBackend *);
int preference_store_summary(PreferenceStore *, PreferenceStoreSummary *);
int preference_store_close(PreferenceStore *);

#endif
