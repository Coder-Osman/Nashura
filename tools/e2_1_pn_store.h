#ifndef E21_PN_STORE_H
#define E21_PN_STORE_H

#include <stddef.h>
#include <stdint.h>

#define E21_PN_TEXT 4096

enum e21_pn_error {
    E21_PN_INVALID = -1,
    E21_PN_CLOSED = -2,
    E21_PN_SMALL = -3,
    E21_PN_IO = -4,
    E21_PN_FORMAT = -5,
    E21_PN_BUSY = -6
};

struct e21_pn_receipt {
    uint32_t changed;
    uint32_t committed;
    uint32_t failed;
    uint32_t renamed;
    int32_t error_o;
    int32_t error_p;
    uint32_t backup_requested;
};

int e21_pn_open(const char *directory, void **output);
int e21_pn_read(void *context, char *output, size_t capacity, struct e21_pn_receipt *receipt);
int e21_pn_write(void *context, const char *value, struct e21_pn_receipt *receipt);
int e21_pn_close(void *context);
void e21_pn_free(void *context);

#endif
