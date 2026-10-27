#define _GNU_SOURCE
#include "preference_cipher.h"
#include "mapped_files.h"

#include <stdio.h>
#include <string.h>

static int read_bytes(FILE *stream, void *data, size_t size) {
    return fread(data, 1, size, stream) == size;
}

static int read_number(FILE *stream, unsigned int *output) {
    unsigned char bytes[4];
    if (!read_bytes(stream, bytes, sizeof(bytes))) return 0;
    *output = (unsigned int)bytes[0] | (unsigned int)bytes[1] << 8 |
              (unsigned int)bytes[2] << 16 | (unsigned int)bytes[3] << 24;
    return 1;
}

static int filled(const unsigned char *bytes, size_t size) {
    for (size_t index = 0; index < size; ++index) if (bytes[index] != 165) return 0;
    return 1;
}

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    FILE *stream = fopen(argv[2], "rb");
    if (!stream) return 65;
    unsigned char magic[4], key[32] = {0}, iv[16] = {0};
    unsigned int count = 0, completed = 0;
    int valid = read_bytes(stream, magic, sizeof(magic)) && !memcmp(magic, "NCS1", 4) &&
        read_bytes(stream, key, sizeof(key)) && read_bytes(stream, iv, sizeof(iv)) &&
        read_number(stream, &count) && count > 0 && count <= 64;
    void *context = NULL;
    if (valid) valid = preference_cipher_open(argv[1], key, sizeof(key), iv, sizeof(iv), &context) == 0;
    if (valid) observe_mapped_files();
    int all_match = valid;
    printf("{\"rows\":[");
    for (unsigned int index = 0; valid && index < count; ++index) {
        unsigned int failed = 0, input_size = 0, expected_size = 0;
        unsigned char input[4112], expected[4112], output[4160];
        size_t output_size = 0;
        valid = read_number(stream, &failed) && failed <= 1 &&
            read_number(stream, &input_size) && input_size <= sizeof(input) &&
            read_number(stream, &expected_size) && expected_size <= sizeof(expected) &&
            (!failed || !expected_size) && read_bytes(stream, input, input_size) &&
            read_bytes(stream, expected, expected_size);
        if (!valid) { all_match = 0; break; }
        memset(output, 165, sizeof(output));
        int result = preference_cipher_transform(context, CIPHER_DECRYPT, input, input_size,
                                                output, sizeof(output), &output_size);
        int guard = output_size <= sizeof(output) &&
            filled(output + output_size, sizeof(output) - output_size);
        int match = result == (failed ? CIPHER_FAILED : CIPHER_OK) && guard &&
            output_size == expected_size && !memcmp(output, expected, expected_size);
        all_match &= match;
        printf("%s{\"result\":%d,\"length\":%zu,\"expected_length\":%u,"
               "\"matched\":%s,\"output_guard\":%s}", completed ? "," : "", result,
               output_size, expected_size, match ? "true" : "false", guard ? "true" : "false");
        ++completed;
    }
    if (valid) valid = fgetc(stream) == EOF && !ferror(stream);
    if (fclose(stream)) valid = 0;
    preference_cipher_close(context);
    volatile unsigned char *secret = key;
    for (size_t index = 0; index < sizeof(key); ++index) secret[index] = 0;
    all_match &= valid;
    printf("],\"status\":\"%s\",\"records\":%u,\"libg_loaded\":false,"
           "\"scope\":\"historical_store1_padding_sequence_bytes_only\"}\n",
           all_match ? "PASS" : "BLOCKED", completed);
    return all_match ? 0 : 2;
}
