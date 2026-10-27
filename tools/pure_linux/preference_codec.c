#define _GNU_SOURCE
#include "preference_codec.h"
#include "preference_cipher.h"

#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

struct PreferenceCodecs { void *slots[2]; pid_t owner; };
static const char alphabet[] = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789+/";

static void wipe(void *memory, size_t length) {
    volatile unsigned char *bytes = memory;
    for (size_t index = 0; index < length; index++) bytes[index] = 0;
}

static int encode(const unsigned char *input, size_t length, char *output, size_t capacity) {
    size_t amount = (length + 2) / 3 * 4;
    if (amount >= capacity) return -1;
    for (size_t index = 0, cursor = 0; index < length; index += 3) {
        unsigned int number = (unsigned int)input[index] << 16;
        if (index + 1 < length) number |= (unsigned int)input[index + 1] << 8;
        if (index + 2 < length) number |= input[index + 2];
        output[cursor++] = alphabet[number >> 18];
        output[cursor++] = alphabet[(number >> 12) & 63];
        output[cursor++] = index + 1 < length ? alphabet[(number >> 6) & 63] : '=';
        output[cursor++] = index + 2 < length ? alphabet[number & 63] : '=';
    }
    output[amount] = 0;
    return 0;
}

static int decode(const char *input, unsigned char *output, size_t *length) {
    size_t amount = strlen(input);
    if (!amount || amount % 4 || amount > 5484) return -1;
    *length = 0;
    for (size_t index = 0; index < amount; index += 4) {
        unsigned int number = 0, padding = 0;
        for (size_t offset = 0; offset < 4; offset++) {
            unsigned char byte = (unsigned char)input[index + offset];
            const char *position = byte ? strchr(alphabet, byte) : NULL;
            if (byte == '=' && index + 4 == amount && offset >= 2) padding++;
            else if (!position || padding) return -1;
            number = (number << 6) | (position ? (unsigned int)(position - alphabet) : 0);
        }
        if (padding > 2 || (padding == 1 && (number & 255)) || (padding == 2 && (number & 65535))) return -1;
        if (*length + 3 - padding > 4112) return -1;
        output[(*length)++] = (unsigned char)(number >> 16);
        if (padding < 2) output[(*length)++] = (unsigned char)(number >> 8);
        if (!padding) output[(*length)++] = (unsigned char)number;
    }
    return 0;
}

static int ascii(const unsigned char *bytes, size_t length) {
    for (size_t index = 0; index < length; index++)
        if (!bytes[index] || bytes[index] > 127) return 0;
    return 1;
}

int preference_codecs_open(const char *library, const unsigned char keys[2][32],
                           const unsigned char iv[16], PreferenceCodecs **output) {
    if (!output) return -1;
    *output = NULL;
    if (!keys || !iv) return -1;
    PreferenceCodecs *codecs = calloc(1, sizeof(*codecs));
    if (!codecs) return -1;
    codecs->owner = getpid();
    for (size_t slot = 0; slot < 2; slot++) {
        if (preference_cipher_open(library, keys[slot], 32, iv, 16, &codecs->slots[slot])) {
            preference_codecs_close(codecs);
            return -1;
        }
    }
    *output = codecs;
    return 0;
}

int preference_codecs_transform(void *handle, unsigned int slot, enum PreferenceCodec operation,
                                const char *input, char *output, size_t capacity) {
    PreferenceCodecs *codecs = handle;
    if (!codecs || codecs->owner != getpid() || slot > 1 || !input || !output ||
        capacity < PREFERENCE_CAPACITY || operation < PREFERENCE_KEY || operation > PREFERENCE_DECRYPT) return -1;
    size_t length = strlen(input), decoded_size = 0, output_size = 0;
    unsigned char decoded[4112], bytes[4144];
    if (operation == PREFERENCE_DECRYPT) {
        if (decode(input, decoded, &decoded_size)) return -1;
    } else if (length > 4096 || !ascii((const unsigned char *)input, length)) return -1;
    enum PreferenceCipherOperation cipher = operation == PREFERENCE_KEY ? CIPHER_KEY :
        operation == PREFERENCE_ENCRYPT ? CIPHER_ENCRYPT : CIPHER_DECRYPT;
    int result = preference_cipher_transform(codecs->slots[slot], cipher,
        operation == PREFERENCE_DECRYPT ? decoded : (const unsigned char *)input,
        operation == PREFERENCE_DECRYPT ? decoded_size : length, bytes, sizeof(bytes), &output_size);
    wipe(decoded, sizeof(decoded));
    if (!result) {
        if (operation != PREFERENCE_DECRYPT) result = encode(bytes, output_size, output, capacity);
        else if (output_size >= capacity || !ascii(bytes, output_size)) result = -1;
        else { memcpy(output, bytes, output_size); output[output_size] = 0; }
    } else result = operation == PREFERENCE_DECRYPT && result == CIPHER_FAILED ? PREFERENCE_DECODE_EXCEPTION : -1;
    wipe(bytes, sizeof(bytes));
    return result;
}

int preference_codecs_open_material(const char *library, const char *path, PreferenceCodecs **output) {
    if (!output) return -1;
    *output = NULL;
    if (!path || path[0] != '/') return -1;
    int descriptor = open(path, O_RDONLY | O_NOFOLLOW | O_CLOEXEC);
    if (descriptor < 0) return -1;
    struct stat status;
    unsigned char material[80], extra;
    int result = -1;
    if (!fstat(descriptor, &status) && S_ISREG(status.st_mode) && status.st_nlink == 1 &&
        status.st_size == sizeof(material) && status.st_uid == geteuid() && !(status.st_mode & 077) &&
        read(descriptor, material, sizeof(material)) == sizeof(material) && read(descriptor, &extra, 1) == 0)
        result = preference_codecs_open(library, (const unsigned char (*)[32])material, material + 64, output);
    if (close(descriptor)) { preference_codecs_close(*output); *output = NULL; result = -1; }
    wipe(material, sizeof(material));
    return result;
}

void preference_codecs_close(PreferenceCodecs *codecs) {
    if (!codecs || codecs->owner != getpid()) return;
    for (size_t slot = 0; slot < 2; slot++) preference_cipher_close(codecs->slots[slot]);
    free(codecs);
}
