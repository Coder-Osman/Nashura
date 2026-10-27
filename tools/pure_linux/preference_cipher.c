#include "preference_cipher.h"

#include <dlfcn.h>
#include <pthread.h>
#include <stdlib.h>
#include <string.h>
#include <sys/types.h>
#include <unistd.h>

typedef struct {
    void *library;
    void *ciphers[3];
    void (*destroy)(void *);
    const void *(*algorithms[3])(void);
    int (*initialize)(void *, const void *, void *, const unsigned char *, const unsigned char *, int);
    int (*update)(void *, unsigned char *, int *, const unsigned char *, int);
    int (*finish)(void *, unsigned char *, int *);
    uint8_t key[32];
    uint8_t iv[16];
    int failed[3];
    pid_t owner;
    pthread_mutex_t mutex;
} Cipher;

static void wipe(void *memory, size_t size) {
    volatile uint8_t *bytes = memory;
    for (size_t index = 0; index < size; ++index) bytes[index] = 0;
}

static int initialize(Cipher *context, unsigned int operation) {
    return context->initialize(context->ciphers[operation], context->algorithms[operation](), NULL,
                               context->key, operation == CIPHER_KEY ? NULL : context->iv,
                               operation != CIPHER_DECRYPT) == 1;
}

int preference_cipher_open(const char *library, const uint8_t *key, size_t key_size,
                           const uint8_t *iv, size_t iv_size, void **output) {
    if (!output) return CIPHER_INVALID;
    *output = NULL;
    if (!library || library[0] != '/' || !key || key_size != 32 || !iv || iv_size != 16)
        return CIPHER_INVALID;
    Cipher *context = calloc(1, sizeof(*context));
    if (!context) return CIPHER_FAILED;
    if (pthread_mutex_init(&context->mutex, NULL)) { free(context); return CIPHER_FAILED; }
    context->owner = getpid();
    context->library = dlopen(library, RTLD_NOW | RTLD_LOCAL);
    if (!context->library) { preference_cipher_close(context); return CIPHER_LINK; }
    void *(*create)(void) = (void *(*)(void))dlsym(context->library, "EVP_CIPHER_CTX_new");
    context->destroy = (void (*)(void *))dlsym(context->library, "EVP_CIPHER_CTX_free");
    context->algorithms[CIPHER_KEY] = (const void *(*)(void))dlsym(context->library, "EVP_aes_256_ecb");
    context->algorithms[CIPHER_ENCRYPT] = (const void *(*)(void))dlsym(context->library, "EVP_aes_256_cbc");
    context->algorithms[CIPHER_DECRYPT] = context->algorithms[CIPHER_ENCRYPT];
    context->initialize = (int (*)(void *, const void *, void *, const unsigned char *, const unsigned char *, int))
        dlsym(context->library, "EVP_CipherInit_ex");
    context->update = (int (*)(void *, unsigned char *, int *, const unsigned char *, int))
        dlsym(context->library, "EVP_CipherUpdate");
    context->finish = (int (*)(void *, unsigned char *, int *))dlsym(context->library, "EVP_CipherFinal_ex");
    if (!create || !context->destroy || !context->algorithms[0] || !context->algorithms[1] ||
        !context->initialize || !context->update || !context->finish) {
        preference_cipher_close(context);
        return CIPHER_LINK;
    }
    memcpy(context->key, key, sizeof(context->key));
    memcpy(context->iv, iv, sizeof(context->iv));
    for (unsigned int operation = 0; operation < 3; ++operation) {
        context->ciphers[operation] = create();
        if (!context->ciphers[operation] || !initialize(context, operation)) {
            preference_cipher_close(context);
            return CIPHER_FAILED;
        }
    }
    *output = context;
    return CIPHER_OK;
}

int preference_cipher_transform(void *handle, enum PreferenceCipherOperation operation,
                                const uint8_t *input, size_t input_size,
                                uint8_t *output, size_t capacity, size_t *output_size) {
    if (!output_size) return CIPHER_INVALID;
    *output_size = 0;
    Cipher *context = handle;
    if (!context || context->owner != getpid() || operation < CIPHER_KEY || operation > CIPHER_DECRYPT ||
        (!input && input_size) || input_size > (operation == CIPHER_DECRYPT ? 4112u : 4096u) ||
        !output) return CIPHER_INVALID;
    if (capacity < input_size + 32) return CIPHER_SMALL;
    pthread_mutex_lock(&context->mutex);
    uint8_t temporary[4128];
    int amount = 0, tail = 0;
    int success = !context->failed[operation] &&
        context->update(context->ciphers[operation], temporary, &amount, input, (int)input_size) == 1 &&
        amount >= 0 && amount <= 4112 &&
        context->finish(context->ciphers[operation], temporary + amount, &tail) == 1 &&
        tail >= 0 && tail <= 16 && (size_t)(amount + tail) <= capacity;
    if (success) {
        if (!initialize(context, (unsigned int)operation)) {
            context->failed[operation] = 1;
            success = 0;
        } else {
            *output_size = (size_t)(amount + tail);
            memcpy(output, temporary, *output_size);
        }
    }
    wipe(temporary, sizeof(temporary));
    pthread_mutex_unlock(&context->mutex);
    return success ? CIPHER_OK : CIPHER_FAILED;
}

void preference_cipher_close(void *handle) {
    Cipher *context = handle;
    if (!context || context->owner != getpid()) return;
    for (unsigned int index = 0; index < 3; ++index)
        if (context->ciphers[index] && context->destroy) context->destroy(context->ciphers[index]);
    if (context->library) dlclose(context->library);
    pthread_mutex_destroy(&context->mutex);
    wipe(context, sizeof(*context));
    free(context);
}
