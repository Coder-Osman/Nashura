#ifndef E21_PN_BRIDGE_H
#define E21_PN_BRIDGE_H

#include <stddef.h>
#include <stdint.h>

#define E21_BRIDGE_TEXT 4096

typedef int (*e21_bridge_read_fn)(void *, uint32_t, char *, size_t);
typedef int (*e21_bridge_write_fn)(void *, uint32_t, const char *);
typedef int (*e21_bridge_backup_fn)(void *);

struct e21_bridge_backend {
    void *context;
    e21_bridge_read_fn read;
    e21_bridge_write_fn write;
    e21_bridge_backup_fn backup;
};

struct e21_bridge_receipt {
    uint32_t reads;
    uint32_t writes;
    uint32_t changed;
    uint32_t backup_returns;
    int32_t failure;
};

int e21_bridge_read(const struct e21_bridge_backend *, char *, size_t, struct e21_bridge_receipt *);
int e21_bridge_write(const struct e21_bridge_backend *, const char *, struct e21_bridge_receipt *);

#endif
