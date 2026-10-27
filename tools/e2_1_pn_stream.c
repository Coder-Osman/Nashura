#include "e2_1_pn_stream.h"
#include "e2_1_pn_codec.h"

#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>

struct e21_stream {
    void *library;
    void *cipher;
    void (*destroy)(void *);
    const void *(*cbc)(void);
    int (*initialize)(void *, const void *, void *, const unsigned char *, const unsigned char *, int);
    int (*update)(void *, unsigned char *, int *, const unsigned char *, int);
    int (*finish)(void *, unsigned char *, int *);
    uint8_t key[32];
    uint8_t iv[16];
};

static void wipe(void *memory, size_t length) {
    volatile uint8_t *bytes = memory;
    for (size_t index = 0; index < length; ++index) bytes[index] = 0;
}

int e21_stream_open(const char *library, const uint8_t *key, size_t key_length,
                    const uint8_t *iv, size_t iv_length, void **output) {
    if (!output) return E21_CODEC_INVALID;
    *output = NULL;
    if (!library || library[0] != '/' || !key || key_length != 32 || !iv || iv_length != 16)
        return E21_CODEC_INVALID;
    struct e21_stream *context = calloc(1, sizeof(*context));
    if (!context) return E21_CODEC_LINK;
    context->library = dlopen(library, RTLD_NOW | RTLD_LOCAL);
    if (!context->library) { free(context); return E21_CODEC_LINK; }
    void *(*create)(void) = (void *(*)(void))dlsym(context->library, "EVP_CIPHER_CTX_new");
    context->destroy = (void (*)(void *))dlsym(context->library, "EVP_CIPHER_CTX_free");
    context->cbc = (const void *(*)(void))dlsym(context->library, "EVP_aes_256_cbc");
    context->initialize = (int (*)(void *, const void *, void *, const unsigned char *, const unsigned char *, int))dlsym(context->library, "EVP_CipherInit_ex");
    context->update = (int (*)(void *, unsigned char *, int *, const unsigned char *, int))dlsym(context->library, "EVP_CipherUpdate");
    context->finish = (int (*)(void *, unsigned char *, int *))dlsym(context->library, "EVP_CipherFinal_ex");
    if (!create || !context->destroy || !context->cbc || !context->initialize || !context->update || !context->finish) {
        e21_stream_close(context);
        return E21_CODEC_LINK;
    }
    memcpy(context->key, key, sizeof(context->key));
    memcpy(context->iv, iv, sizeof(context->iv));
    context->cipher = create();
    if (!context->cipher || context->initialize(context->cipher, context->cbc(), NULL, key, iv, 0) != 1) {
        e21_stream_close(context);
        return E21_CODEC_CRYPTO;
    }
    *output = context;
    return 0;
}

int e21_stream_decode(void *handle, const uint8_t *input, size_t input_length,
                      uint8_t *output, size_t capacity, size_t *output_length) {
    if (!output_length) return E21_CODEC_INVALID;
    *output_length = 0;
    if (!handle || (!input && input_length) || input_length > 4096 || !output) return E21_CODEC_INVALID;
    if (capacity < input_length + 32) return E21_CODEC_SMALL;
    struct e21_stream *context = handle;
    uint8_t temporary[4128];
    int amount = 0, tail = 0;
    int success = context->update(context->cipher, temporary, &amount, input, (int)input_length) == 1
        && amount >= 0 && amount <= 4112
        && context->finish(context->cipher, temporary + amount, &tail) == 1
        && tail >= 0 && tail <= 16 && (size_t)(amount + tail) <= capacity;
    if (success) {
        success = context->initialize(context->cipher, context->cbc(), NULL, context->key, context->iv, 0) == 1;
        if (success) {
            *output_length = (size_t)(amount + tail);
            memcpy(output, temporary, *output_length);
        }
    }
    wipe(temporary, sizeof(temporary));
    return success ? 0 : E21_CODEC_CRYPTO;
}

void e21_stream_close(void *handle) {
    struct e21_stream *context = handle;
    if (!context) return;
    if (context->cipher && context->destroy) context->destroy(context->cipher);
    if (context->library) dlclose(context->library);
    wipe(context, sizeof(*context));
    free(context);
}
