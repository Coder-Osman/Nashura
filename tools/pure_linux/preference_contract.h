#ifndef NASHURA_PREFERENCE_CONTRACT_H
#define NASHURA_PREFERENCE_CONTRACT_H

#include <stddef.h>
#include <stdint.h>

#define PREFERENCE_CAPACITY 8192
#define PREFERENCE_DECODE_EXCEPTION 1

enum PreferenceCodec { PREFERENCE_KEY, PREFERENCE_ENCRYPT, PREFERENCE_DECRYPT };
enum PreferenceEdit { PREFERENCE_PUT, PREFERENCE_REMOVE };

typedef struct {
    void *context;
    int (*codec)(void *, unsigned int, enum PreferenceCodec, const char *, char *, size_t);
    int (*contains)(void *, unsigned int, const char *, int *);
    int (*get_string)(void *, unsigned int, const char *, char *, size_t);
    int (*edit)(void *, unsigned int, uint64_t *);
    int (*change)(void *, uint64_t, enum PreferenceEdit, const char *, const char *);
    int (*commit)(void *, uint64_t, int *);
    int (*decode_exception)(void *, unsigned int);
    int (*request_backup)(void *);
} PreferenceBackend;

int preference_contract_read(const PreferenceBackend *, const char *, char *, size_t);
int preference_contract_write(const PreferenceBackend *, const char *, const char *);

#endif
