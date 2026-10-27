#ifndef NASHURA_PREFERENCE_CODEC_H
#define NASHURA_PREFERENCE_CODEC_H

#include "preference_contract.h"

typedef struct PreferenceCodecs PreferenceCodecs;

int preference_codecs_open(const char *, const unsigned char [2][32], const unsigned char [16], PreferenceCodecs **);
int preference_codecs_open_material(const char *, const char *, PreferenceCodecs **);
int preference_codecs_transform(void *, unsigned int, enum PreferenceCodec, const char *, char *, size_t);
void preference_codecs_close(PreferenceCodecs *);

#endif
