#ifndef E21_PN_DERIVE_H
#define E21_PN_DERIVE_H

#include <stddef.h>
#include <stdint.h>

enum { E21_DERIVE_INVALID = -1, E21_DERIVE_LINK = -2, E21_DERIVE_HASH = -3 };

int e21_pn_derive(const char *crypto_library,
                  const uint8_t *app_identity, size_t identity_length,
                  const uint8_t *package, size_t package_length,
                  const int32_t *table, size_t table_count,
                  const uint8_t *iv_source, size_t iv_source_length,
                  uint8_t *keys, size_t keys_capacity,
                  uint8_t *iv, size_t iv_capacity);

#endif
