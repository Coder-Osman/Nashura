#ifndef NASHURA_PREFERENCE_CIPHER_H
#define NASHURA_PREFERENCE_CIPHER_H

#include <stddef.h>
#include <stdint.h>

enum PreferenceCipherOperation { CIPHER_KEY, CIPHER_ENCRYPT, CIPHER_DECRYPT };
enum PreferenceCipherResult { CIPHER_OK, CIPHER_INVALID = -1, CIPHER_LINK = -2,
                              CIPHER_FAILED = -3, CIPHER_SMALL = -4 };

int preference_cipher_open(const char *, const uint8_t *, size_t, const uint8_t *, size_t, void **);
int preference_cipher_transform(void *, enum PreferenceCipherOperation, const uint8_t *, size_t,
                                uint8_t *, size_t, size_t *);
void preference_cipher_close(void *);

#endif
