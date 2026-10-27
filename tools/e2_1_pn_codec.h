#ifndef E21_PN_CODEC_H
#define E21_PN_CODEC_H

#include <stddef.h>
#include <stdint.h>

enum e21_codec_error {
    E21_CODEC_INVALID = -1,
    E21_CODEC_LINK = -2,
    E21_CODEC_SMALL = -3,
    E21_CODEC_CRYPTO = -4
};

int e21_codec_open(const char *library, void **output);
int e21_codec_transform(void *context, int mode, int encrypt,
                       const uint8_t *key, size_t key_length,
                       const uint8_t *iv, size_t iv_length,
                       const uint8_t *input, size_t input_length,
                       uint8_t *output, size_t capacity, size_t *output_length);
void e21_codec_close(void *context);

#endif
