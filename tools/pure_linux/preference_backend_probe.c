#define _GNU_SOURCE
#include "preference_codec.h"
#include "preference_store.h"
#include "mapped_files.h"

#include <inttypes.h>
#include <stdio.h>
#include <string.h>

static unsigned int backup_calls, exception_calls;

static int unresolved_backup(void *context) {
    (void)context;
    backup_calls++;
    return -1;
}

static int unresolved_exception(void *context, unsigned int slot) {
    (void)context; (void)slot;
    exception_calls++;
    return -1;
}

static int number(FILE *stream, unsigned int *result) {
    unsigned char bytes[4];
    if (fread(bytes, 1, 4, stream) != 4) return -1;
    *result = (unsigned int)bytes[0] | (unsigned int)bytes[1] << 8 |
        (unsigned int)bytes[2] << 16 | (unsigned int)bytes[3] << 24;
    return 0;
}

static int references(PreferenceCodecs *codecs, const char *path, unsigned int *completed) {
    FILE *stream = fopen(path, "rb");
    if (!stream) return -1;
    char magic[4];
    unsigned int count;
    int valid = fread(magic, 1, 4, stream) == 4 && !memcmp(magic, "NCP1", 4) &&
        !number(stream, &count) && count > 0 && count <= 64;
    *completed = 0;
    for (unsigned int index = 0; valid && index < count; index++) {
        unsigned int slot, operation, input_size, expected_size;
        char input[PREFERENCE_CAPACITY], expected[PREFERENCE_CAPACITY], actual[PREFERENCE_CAPACITY];
        valid = !number(stream, &slot) && slot < 2 && !number(stream, &operation) && operation < 3 &&
            !number(stream, &input_size) && input_size < sizeof(input) &&
            !number(stream, &expected_size) && expected_size < sizeof(expected) &&
            fread(input, 1, input_size, stream) == input_size &&
            fread(expected, 1, expected_size, stream) == expected_size;
        if (!valid) break;
        input[input_size] = expected[expected_size] = 0;
        memset(actual, 165, sizeof(actual));
        valid = !memchr(input, 0, input_size) && !memchr(expected, 0, expected_size) &&
            !preference_codecs_transform(codecs, slot, (enum PreferenceCodec)operation,
                                         input, actual, sizeof(actual)) &&
            !memcmp(actual, expected, expected_size + 1);
        for (size_t offset = expected_size + 1; valid && offset < sizeof(actual); offset++)
            if ((unsigned char)actual[offset] != 165) valid = 0;
        if (valid) (*completed)++;
    }
    if (valid) valid = fgetc(stream) == EOF && !ferror(stream);
    if (fclose(stream)) valid = 0;
    return valid ? 0 : -1;
}

int main(int argc, char **argv) {
    if (argc != 5) return 64;
    PreferenceCodecs *codecs = NULL;
    PreferenceStore *store = NULL;
    PreferenceBackend backend;
    PreferenceStoreSummary summary;
    unsigned int compared = 0;
    int valid = !preference_codecs_open_material(argv[1], argv[2], &codecs);
    if (valid) observe_mapped_files();
    valid = valid && !references(codecs, argv[3], &compared);
    PreferenceStoreOptions options = {codecs, preference_codecs_transform, unresolved_exception,
                                     unresolved_backup, NULL, NULL};
    valid = valid && !preference_store_open(argv[4], &options, &store) &&
        !preference_store_backend(store, &backend);
    const char *key = "LOADING_SCREEN_STATE_LABEL", *value = "nashura-isolated-persistence-check";
    int write_result = valid ? preference_contract_write(&backend, key, value) : -2;
    valid = valid && write_result == -1 && backup_calls == 1 && exception_calls == 0 &&
        !preference_store_summary(store, &summary) && summary.commits == 2 &&
        summary.disk_writes == 2 && summary.false_commits == 0 && summary.backup_failures == 1;
    if (store) { if (preference_store_close(store)) valid = 0; store = NULL; }
    char output[PREFERENCE_CAPACITY];
    valid = valid && !preference_store_open(argv[4], &options, &store) &&
        !preference_store_backend(store, &backend) &&
        !preference_contract_read(&backend, key, output, sizeof(output)) && !strcmp(output, value) &&
        !preference_contract_write(&backend, key, value) && backup_calls == 1;
    if (store && preference_store_close(store)) valid = 0;
    preference_codecs_close(codecs);
    printf("{\"status\":\"%s\",\"historical_ascii_references\":%u,\"two_slot_commits\":%s,"
           "\"reopen_read_equal\":%s,\"backup_requests\":%u,\"backup_service\":\"UNRESOLVED\","
           "\"decode_exceptions\":%u,\"libg_loaded\":false,\"catalog_ready_pass\":false,"
           "\"scope\":\"historical_ascii_codec_and_xml_persistence_only\"}\n",
           valid ? "PASS" : "BLOCKED", compared, valid ? "true" : "false",
           valid ? "true" : "false", backup_calls, exception_calls);
    return valid ? 0 : 2;
}
