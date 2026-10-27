#include "e2_1_pn_codec.h"

#include <dlfcn.h>
#include <stdlib.h>
#include <string.h>

struct e21_codec {
    void *library;
    void *(*create)(void);
    void (*destroy)(void *);
    const void *(*ecb)(void);
    const void *(*cbc)(void);
    int (*initialize)(void *, const void *, void *, const unsigned char *, const unsigned char *, int);
    int (*update)(void *, unsigned char *, int *, const unsigned char *, int);
    int (*finish)(void *, unsigned char *, int *);
};

int e21_codec_open(const char *library, void **output) {
    if (!output) return E21_CODEC_INVALID;
    *output = NULL;
    if (!library || library[0] != '/') return E21_CODEC_INVALID;
    struct e21_codec *context = calloc(1, sizeof(*context));
    if (!context) return E21_CODEC_LINK;
    context->library = dlopen(library, RTLD_NOW | RTLD_LOCAL);
    if (!context->library) { free(context); return E21_CODEC_LINK; }
    context->create = (void *(*)(void))dlsym(context->library, "EVP_CIPHER_CTX_new");
    context->destroy = (void (*)(void *))dlsym(context->library, "EVP_CIPHER_CTX_free");
    context->ecb = (const void *(*)(void))dlsym(context->library, "EVP_aes_256_ecb");
    context->cbc = (const void *(*)(void))dlsym(context->library, "EVP_aes_256_cbc");
    context->initialize = (int (*)(void *, const void *, void *, const unsigned char *, const unsigned char *, int))dlsym(context->library, "EVP_CipherInit_ex");
    context->update = (int (*)(void *, unsigned char *, int *, const unsigned char *, int))dlsym(context->library, "EVP_CipherUpdate");
    context->finish = (int (*)(void *, unsigned char *, int *))dlsym(context->library, "EVP_CipherFinal_ex");
    if (!context->create || !context->destroy || !context->ecb || !context->cbc ||
        !context->initialize || !context->update || !context->finish) {
        e21_codec_close(context);
        return E21_CODEC_LINK;
    }
    *output = context;
    return 0;
}

int e21_codec_transform(void *handle, int mode, int encrypt,
                       const uint8_t *key, size_t key_length,
                       const uint8_t *iv, size_t iv_length,
                       const uint8_t *input, size_t input_length,
                       uint8_t *output, size_t capacity, size_t *output_length) {
    if (!output_length) return E21_CODEC_INVALID;
    *output_length = 0;
    if (!handle || !key || key_length != 32 || !output || (!input && input_length) ||
        input_length > (encrypt ? 4095u : 4096u) || (mode != 0 && mode != 1) || (encrypt != 0 && encrypt != 1) ||
        (mode == 0 && iv_length != 0) || (mode == 1 && (!iv || iv_length != 16)) ||
        (!encrypt && (!input_length || input_length % 16))) return E21_CODEC_INVALID;
    if (capacity < input_length + 16) return E21_CODEC_SMALL;
    struct e21_codec *context = handle;
    void *cipher = context->create();
    if (!cipher) return E21_CODEC_CRYPTO;
    uint8_t temporary[4128];
    int amount = 0;
    int tail = 0;
    int success = context->initialize(cipher, mode ? context->cbc() : context->ecb(), NULL,
                                      key, mode ? iv : NULL, encrypt) == 1 &&
                  context->update(cipher, temporary, &amount, input, (int)input_length) == 1 &&
                  amount >= 0 && amount <= 4112 &&
                  context->finish(cipher, temporary + amount, &tail) == 1 &&
                  tail >= 0 && tail <= 16 && (size_t)(amount + tail) <= capacity;
    context->destroy(cipher);
    if (success) {
        *output_length = (size_t)(amount + tail);
        memcpy(output, temporary, *output_length);
    }
    volatile uint8_t *wipe = temporary;
    for (size_t index = 0; index < sizeof(temporary); ++index) wipe[index] = 0;
    return success ? 0 : E21_CODEC_CRYPTO;
}

void e21_codec_close(void *handle) {
    struct e21_codec *context = handle;
    if (!context) return;
    if (context->library) dlclose(context->library);
    free(context);
}
