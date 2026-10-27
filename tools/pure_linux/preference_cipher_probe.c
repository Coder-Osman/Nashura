#define _GNU_SOURCE
#include "preference_cipher.h"
#include "mapped_files.h"

#include <link.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

static const uint8_t vector_key[32] = {
    0x60,0x3d,0xeb,0x10,0x15,0xca,0x71,0xbe,0x2b,0x73,0xae,0xf0,0x85,0x7d,0x77,0x81,
    0x1f,0x35,0x2c,0x07,0x3b,0x61,0x08,0xd7,0x2d,0x98,0x10,0xa3,0x09,0x14,0xdf,0xf4
};
static const uint8_t vector_iv[16] = {0,1,2,3,4,5,6,7,8,9,10,11,12,13,14,15};
static const uint8_t vector_plain[16] = {
    0x6b,0xc1,0xbe,0xe2,0x2e,0x40,0x9f,0x96,0xe9,0x3d,0x7e,0x11,0x73,0x93,0x17,0x2a
};
static const uint8_t vector_cbc[16] = {
    0xf5,0x8c,0x4c,0x04,0xd6,0xe5,0xf1,0xba,0x77,0x9e,0xab,0xfb,0x5f,0x7b,0xfb,0xd6
};
static const uint8_t vector_ecb[16] = {
    0xf3,0xee,0xd1,0xbd,0xb5,0xd2,0xa0,0x3c,0x06,0x4b,0x5a,0x7e,0x3d,0xb1,0x81,0xf8
};

static int open_cipher(const char *library, const uint8_t *iv, void **handle) {
    return preference_cipher_open(library, vector_key, sizeof(vector_key), iv, 16, handle);
}

static int boundaries(const char *library, const uint8_t *iv) {
    const size_t sizes[] = {0,1,15,16,17,31,32,127,4095,4096};
    uint8_t plain[4096], encrypted[4128], repeated[4128], decoded[4160];
    for (size_t index = 0; index < sizeof(plain); ++index) plain[index] = (uint8_t)(index * 31);
    void *handle = NULL;
    if (open_cipher(library, iv, &handle)) return 1;
    int failed = 0;
    for (size_t index = 0; index < sizeof(sizes) / sizeof(sizes[0]); ++index) {
        size_t length = sizes[index], encrypted_size = 0, repeated_size = 0, decoded_size = 0;
        if (preference_cipher_transform(handle, CIPHER_ENCRYPT, plain, length,
                                        encrypted, sizeof(encrypted), &encrypted_size) ||
            encrypted_size != (length / 16 + 1) * 16 ||
            preference_cipher_transform(handle, CIPHER_ENCRYPT, plain, length,
                                        repeated, sizeof(repeated), &repeated_size) ||
            repeated_size != encrypted_size || memcmp(repeated, encrypted, encrypted_size) ||
            preference_cipher_transform(handle, CIPHER_DECRYPT, encrypted, encrypted_size,
                                        decoded, sizeof(decoded), &decoded_size) ||
            decoded_size != length || memcmp(decoded, plain, length)) { failed = 1; break; }
        if (preference_cipher_transform(handle, CIPHER_KEY, plain, length,
                                        encrypted, sizeof(encrypted), &encrypted_size) ||
            preference_cipher_transform(handle, CIPHER_KEY, plain, length,
                                        repeated, sizeof(repeated), &repeated_size) ||
            encrypted_size != repeated_size || memcmp(encrypted, repeated, encrypted_size)) { failed = 1; break; }
    }
    preference_cipher_close(handle);
    return failed;
}

static int sequence(const char *library, int corruption) {
    void *handle = NULL;
    if (open_cipher(library, vector_iv, &handle)) return 1;
    uint8_t ciphertext[64], output[96], damaged[64];
    size_t ciphertext_size = 0;
    if (preference_cipher_transform(handle, CIPHER_ENCRYPT, vector_plain, sizeof(vector_plain),
                                    ciphertext, sizeof(ciphertext), &ciphertext_size)) {
        preference_cipher_close(handle);
        return 1;
    }
    memcpy(damaged, ciphertext, ciphertext_size);
    if (corruption) damaged[15] ^= 1;
    int failed = 0;
    printf("%s[", corruption ? ",\"bad_padding\":" : ",\"nonaligned\":");
    for (unsigned int index = 0; index < 4; ++index) {
        memset(output, 0xa5, sizeof(output));
        size_t output_size = 123;
        int result = preference_cipher_transform(handle, CIPHER_DECRYPT,
            index ? ciphertext : damaged, index || corruption ? ciphertext_size : 1,
            output, sizeof(output), &output_size);
        int matches = result == 0 && output_size == sizeof(vector_plain) &&
                      !memcmp(output, vector_plain, sizeof(vector_plain));
        int unchanged = 1;
        for (size_t offset = 0; offset < sizeof(output); ++offset)
            if (output[offset] != 0xa5) unchanged = 0;
        printf("%s{\"result\":%d,\"length\":%zu,\"matches\":%s,\"output_unchanged\":%s}",
               index ? "," : "", result, output_size, matches ? "true" : "false", unchanged ? "true" : "false");
        if (!index && result != CIPHER_FAILED) failed = 1;
        if (result && (output_size || !unchanged)) failed = 1;
    }
    printf("]");
    uint8_t check[64];
    size_t check_size = 0;
    if (preference_cipher_transform(handle, CIPHER_KEY, vector_plain, sizeof(vector_plain),
                                    check, sizeof(check), &check_size) || check_size != 32 ||
        memcmp(check, vector_ecb, 16)) failed = 1;
    preference_cipher_close(handle);
    return failed;
}

int main(int argc, char **argv) {
    if (argc != 3) return 64;
    uint8_t real_iv[16];
    FILE *input = fopen(argv[2], "rb");
    if (!input) return 65;
    size_t amount = fread(real_iv, 1, sizeof(real_iv), input);
    int extra = fgetc(input);
    int input_error = ferror(input);
    int close_error = fclose(input);
    if (amount != sizeof(real_iv) || extra != EOF || input_error || close_error) return 65;
    void *handle = NULL;
    if (open_cipher(argv[1], vector_iv, &handle)) return 66;
    observe_mapped_files();
    uint8_t ciphertext[64];
    size_t ciphertext_size = 0;
    int failed = preference_cipher_transform(handle, CIPHER_ENCRYPT, vector_plain, sizeof(vector_plain),
                                            ciphertext, sizeof(ciphertext), &ciphertext_size) ||
                 ciphertext_size != 32 || memcmp(ciphertext, vector_cbc, 16);
    failed |= preference_cipher_transform(handle, CIPHER_KEY, vector_plain, sizeof(vector_plain),
                                          ciphertext, sizeof(ciphertext), &ciphertext_size) ||
              ciphertext_size != 32 || memcmp(ciphertext, vector_ecb, 16);
    preference_cipher_close(handle);
    failed |= boundaries(argv[1], vector_iv);
    failed |= boundaries(argv[1], real_iv);
    printf("{\"scope\":\"primitive_candidate_public_key_real_dex_iv\",\"libg_loaded\":false,"
           "\"known_answer_first_blocks\":2,\"boundary_cases\":20");
    failed |= sequence(argv[1], 0);
    failed |= sequence(argv[1], 1);
    printf(",\"status\":\"%s\"}\n", failed ? "FAIL" : "PASS");
    return failed ? 2 : 0;
}
