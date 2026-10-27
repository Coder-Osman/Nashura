#ifndef E21_PN_STREAM_H
#define E21_PN_STREAM_H

#include <stddef.h>
#include <stdint.h>

int e21_stream_open(const char *library, const uint8_t *key, size_t key_length,
                    const uint8_t *iv, size_t iv_length, void **output);
int e21_stream_decode(void *handle, const uint8_t *input, size_t input_length,
                      uint8_t *output, size_t capacity, size_t *output_length);
void e21_stream_close(void *handle);

#endif
