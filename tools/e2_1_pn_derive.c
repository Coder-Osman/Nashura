#include "e2_1_pn_derive.h"

#include <dlfcn.h>
#include <string.h>

static void erase(uint8_t *buffer, size_t length) {
    volatile uint8_t *target = buffer;
    for (size_t index = 0; index < length; ++index) target[index] = 0;
}

static int overlaps(const void *first, size_t first_length, const void *second, size_t second_length) {
    uintptr_t left = (uintptr_t)first, right = (uintptr_t)second;
    return left <= right ? right - left < first_length : left - right < second_length;
}

int e21_pn_derive(const char *crypto_library,
                  const uint8_t *app_identity, size_t identity_length,
                  const uint8_t *package, size_t package_length,
                  const int32_t *table, size_t table_count,
                  const uint8_t *iv_source, size_t iv_source_length,
                  uint8_t *keys, size_t keys_capacity,
                  uint8_t *iv, size_t iv_capacity) {
    if (!crypto_library || crypto_library[0] != '/' || !app_identity || identity_length != 16 ||
        !package || !package_length || package_length > 255 || !table || table_count != 24 ||
        !iv_source || iv_source_length < 16 || iv_source_length > 4095 ||
        !keys || keys_capacity < 64 || !iv || iv_capacity < 16) return E21_DERIVE_INVALID;
    const void *inputs[] = {app_identity, package, table, iv_source};
    size_t lengths[] = {identity_length, package_length, table_count * sizeof(*table), iv_source_length};
    if (overlaps(keys, 64, iv, 16)) return E21_DERIVE_INVALID;
    for (size_t index = 0; index < 4; ++index) {
        if (overlaps(keys, 64, inputs[index], lengths[index]) || overlaps(iv, 16, inputs[index], lengths[index])) {
            return E21_DERIVE_INVALID;
        }
    }
    for (size_t index = 0; index < identity_length; ++index) {
        uint8_t value = app_identity[index];
        if (!((value >= '0' && value <= '9') || (value >= 'a' && value <= 'f'))) return E21_DERIVE_INVALID;
    }
    for (size_t index = 0; index < package_length; ++index) {
        uint8_t value = package[index];
        if (!((value >= 'a' && value <= 'z') || (value >= 'A' && value <= 'Z') ||
              (value >= '0' && value <= '9') || value == '.' || value == '_')) return E21_DERIVE_INVALID;
    }
    void *library = dlopen(crypto_library, RTLD_NOW | RTLD_LOCAL);
    if (!library) return E21_DERIVE_LINK;
    unsigned char *(*sha256)(const unsigned char *, size_t, unsigned char *) =
        (unsigned char *(*)(const unsigned char *, size_t, unsigned char *))dlsym(library, "SHA256");
    if (!sha256) { dlclose(library); return E21_DERIVE_LINK; }
    uint8_t transformed[255];
    uint8_t derived[64];
    for (size_t ordinal = 0; ordinal < package_length; ++ordinal) {
        size_t index = package_length - 1 - ordinal;
        transformed[ordinal] = (uint8_t)((((uint32_t)package[index] ^ (uint32_t)table[index % 24]) & 31u) + 48u);
    }
    int success = sha256(app_identity, identity_length, derived) == derived &&
        sha256(transformed, package_length, derived + 32) == derived + 32;
    if (success) {
        memcpy(keys, derived, sizeof(derived));
        memcpy(iv, iv_source, 16);
    }
    erase(transformed, sizeof(transformed));
    erase(derived, sizeof(derived));
    dlclose(library);
    return success ? 0 : E21_DERIVE_HASH;
}
