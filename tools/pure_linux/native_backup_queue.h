#ifndef NASHURA_NATIVE_BACKUP_QUEUE_H
#define NASHURA_NATIVE_BACKUP_QUEUE_H

typedef struct NativeBackupQueue NativeBackupQueue;

int native_backup_queue_open(const char *, NativeBackupQueue **);
int native_backup_queue_request(NativeBackupQueue *, int *);
int native_backup_queue_close(NativeBackupQueue *);

#endif
