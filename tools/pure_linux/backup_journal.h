#ifndef NASHURA_BACKUP_JOURNAL_H
#define NASHURA_BACKUP_JOURNAL_H

#include <stddef.h>
#include <stdint.h>

int backup_journal_append_utf16(const char *path, const uint16_t *units, size_t count);

#endif
