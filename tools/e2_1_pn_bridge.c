#include "e2_1_pn_bridge.h"

#include <string.h>

static int valid_text(const char *value, size_t capacity) {
    if (!value || !capacity) return 0;
    const unsigned char *bytes = (const unsigned char *)value;
    size_t index = 0;
    while (index < capacity && bytes[index]) {
        unsigned char first = bytes[index++];
        if (first < 0x80) continue;
        size_t extra;
        uint32_t code;
        uint32_t minimum;
        if (first >= 0xc2 && first <= 0xdf) { extra = 1; code = first & 0x1f; minimum = 0x80; }
        else if (first >= 0xe0 && first <= 0xef) { extra = 2; code = first & 0x0f; minimum = 0x800; }
        else if (first >= 0xf0 && first <= 0xf4) { extra = 3; code = first & 7; minimum = 0x10000; }
        else return 0;
        if (extra > capacity - index) return 0;
        for (size_t offset = 0; offset < extra; ++offset) {
            unsigned char next = bytes[index++];
            if (next < 0x80 || next > 0xbf) return 0;
            code = (code << 6) | (next & 0x3f);
        }
        if (code < minimum || code > 0x10ffff || (code >= 0xd800 && code <= 0xdfff)) return 0;
    }
    return index < capacity;
}

static int begin(const struct e21_bridge_backend *backend, struct e21_bridge_receipt *receipt) {
    if (!receipt) return -1;
    memset(receipt, 0, sizeof(*receipt));
    if (!backend || !backend->read || !backend->write || !backend->backup) {
        receipt->failure = -1;
        return -1;
    }
    return 0;
}

static int read_slot(const struct e21_bridge_backend *backend, uint32_t slot, char *value,
                     struct e21_bridge_receipt *receipt) {
    memset(value, 0xff, E21_BRIDGE_TEXT);
    ++receipt->reads;
    int status = backend->read(backend->context, slot, value, E21_BRIDGE_TEXT);
    if (status) return receipt->failure = status;
    if (!valid_text(value, E21_BRIDGE_TEXT)) return receipt->failure = -2;
    return 0;
}

static int write_slot(const struct e21_bridge_backend *backend, uint32_t slot, const char *value,
                      struct e21_bridge_receipt *receipt) {
    ++receipt->writes;
    int status = backend->write(backend->context, slot, value);
    if (status) return receipt->failure = status;
    receipt->changed |= 1u << slot;
    return 0;
}

static int backup(const struct e21_bridge_backend *backend, struct e21_bridge_receipt *receipt) {
    int status = backend->backup(backend->context);
    if (status) return receipt->failure = status;
    ++receipt->backup_returns;
    return 0;
}

int e21_bridge_read(const struct e21_bridge_backend *backend, char *output, size_t capacity,
                    struct e21_bridge_receipt *receipt) {
    if (begin(backend, receipt)) return -1;
    if (!output || capacity < E21_BRIDGE_TEXT) return receipt->failure = -1;
    char value[E21_BRIDGE_TEXT];
    int status = read_slot(backend, 1, value, receipt);
    if (status) return status;
    if (!value[0]) {
        status = read_slot(backend, 0, value, receipt);
        if (status) return status;
        if (value[0]) {
            status = write_slot(backend, 1, value, receipt);
            if (status) return status;
            status = backup(backend, receipt);
            if (status) return status;
        }
    }
    memcpy(output, value, strlen(value) + 1);
    return 0;
}

int e21_bridge_write(const struct e21_bridge_backend *backend, const char *value,
                     struct e21_bridge_receipt *receipt) {
    if (begin(backend, receipt)) return -1;
    if (!valid_text(value, E21_BRIDGE_TEXT)) return receipt->failure = -1;
    char previous[E21_BRIDGE_TEXT];
    for (uint32_t slot = 0; slot < 2; ++slot) {
        int status = read_slot(backend, slot, previous, receipt);
        if (status) return status;
        if (strcmp(previous, value)) {
            status = write_slot(backend, slot, value, receipt);
            if (status) return status;
        }
    }
    return receipt->changed ? backup(backend, receipt) : 0;
}
