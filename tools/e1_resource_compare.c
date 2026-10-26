#define _POSIX_C_SOURCE 200809L
#define _FILE_OFFSET_BITS 64
#include <android/asset_manager.h>
#include <inttypes.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

extern void *e1_AAssetManager_fromJava(void *, void *);
extern void *e1_AAssetManager_open(void *, const char *, int);
extern int e1_AAsset_read(void *, void *, size_t);
extern int64_t e1_AAsset_seek64(void *, int64_t, int);
extern int64_t e1_AAsset_getLength64(void *);
extern int64_t e1_AAsset_getRemainingLength64(void *);
extern int e1_AAsset_openFileDescriptor64(void *, int64_t *, int64_t *);
extern void e1_AAsset_close(void *);

int e1_compare_resources(void *stock_manager, const char *index_path, const char *receipt_path) {
    FILE *index = fopen(index_path, "r");
    FILE *receipt = fopen(receipt_path, "wx");
    if (!index || !receipt || !stock_manager) {
        if (index) fclose(index);
        if (receipt) fclose(receipt);
        return -1;
    }
    void *replacement_manager = e1_AAssetManager_fromJava(NULL, NULL);
    uint64_t files = 0, bytes = 0, failures = 0, descriptor_checks = 0, missing_checks = 0;
    char *line = NULL;
    size_t capacity = 0;
    unsigned char original[65536], replacement[65536];
    while (getline(&line, &capacity, index) >= 0) {
        char *separator = strchr(line, '\t');
        if (!separator) { failures++; break; }
        *separator = 0;
        AAsset *stock = AAssetManager_open(stock_manager, line, AASSET_MODE_RANDOM);
        void *asset = e1_AAssetManager_open(replacement_manager, line, AASSET_MODE_RANDOM);
        uint64_t before = failures;
        if (!stock || !asset) {
            failures++;
        } else {
            int64_t length = AAsset_getLength64(stock);
            if (length != e1_AAsset_getLength64(asset)) failures++;
            for (;;) {
                int amount = AAsset_read(stock, original, sizeof(original));
                int received = e1_AAsset_read(asset, replacement, sizeof(replacement));
                if (amount != received || amount < 0 || (amount > 0 && memcmp(original, replacement, (size_t)amount))) {
                    failures++; break;
                }
                if (AAsset_getRemainingLength64(stock) != e1_AAsset_getRemainingLength64(asset)) failures++;
                if (!amount) break;
                bytes += (uint64_t)amount;
            }
            const int64_t offsets[] = {0, 0, length ? -1 : 0, 1, -1, 0};
            const int origins[] = {SEEK_SET, SEEK_END, SEEK_END, SEEK_END, SEEK_SET, 99};
            for (size_t position = 0; position < sizeof(offsets) / sizeof(offsets[0]); position++) {
                int64_t first = AAsset_seek64(stock, offsets[position], origins[position]);
                int64_t second = e1_AAsset_seek64(asset, offsets[position], origins[position]);
                if (first != second || AAsset_getRemainingLength64(stock) != e1_AAsset_getRemainingLength64(asset)) failures++;
                int amount = AAsset_read(stock, original, 7);
                int received = e1_AAsset_read(asset, replacement, 7);
                if (amount != received || amount < 0 || (amount > 0 && memcmp(original, replacement, (size_t)amount))) failures++;
            }
            off64_t stock_start = -1, stock_length = -1;
            int64_t replacement_start = -1, replacement_length = -1;
            int stock_fd = AAsset_openFileDescriptor64(stock, &stock_start, &stock_length);
            int replacement_fd = e1_AAsset_openFileDescriptor64(asset, &replacement_start, &replacement_length);
            if ((stock_fd >= 0) != (replacement_fd >= 0)) failures++;
            if (stock_fd >= 0 && replacement_fd >= 0) {
                if (stock_start != replacement_start || stock_length != replacement_length || stock_length != length) failures++;
                ssize_t first = pread(stock_fd, original, sizeof(original), stock_start);
                ssize_t second = pread(replacement_fd, replacement, sizeof(replacement), replacement_start);
                if (first != second || first < 0 || (first > 0 && memcmp(original, replacement, (size_t)first))) failures++;
            }
            if (stock_fd >= 0) close(stock_fd);
            if (replacement_fd >= 0) close(replacement_fd);
            descriptor_checks++;
        }
        if (stock) AAsset_close(stock);
        if (asset) e1_AAsset_close(asset);
        if (before != failures) fprintf(receipt, "mismatch\t%s\t%" PRIu64 "\n", line, failures - before);
        files++;
    }
    if (ferror(index)) failures++;
    const char *missing[] = {"__e1_missing_resource_fixture__", "__e1_missing_resource_fixture__/child"};
    for (size_t position = 0; position < sizeof(missing) / sizeof(missing[0]); position++) {
        AAsset *stock = AAssetManager_open(stock_manager, missing[position], AASSET_MODE_UNKNOWN);
        void *asset = e1_AAssetManager_open(replacement_manager, missing[position], AASSET_MODE_UNKNOWN);
        if (stock || asset) failures++;
        if (stock) AAsset_close(stock);
        if (asset) e1_AAsset_close(asset);
        missing_checks++;
    }
    fprintf(receipt, "summary\tfiles=%" PRIu64 "\tbytes=%" PRIu64 "\tfailures=%" PRIu64
            "\tdescriptors=%" PRIu64 "\tmissing=%" PRIu64 "\n",
            files, bytes, failures, descriptor_checks, missing_checks);
    free(line); fclose(index);
    int failed = fclose(receipt) != 0;
    return failed || failures ? 1 : 0;
}
