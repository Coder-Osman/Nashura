/* Current component combination plus exactly one query-block experiment. */
#ifndef NASHURA_HEADLESS_COMBINED_SIGNED_HEIGHT_H
#define NASHURA_HEADLESS_COMBINED_SIGNED_HEIGHT_H
#include "headless_combined_component_guard.h"
#include "headless_query_signed_height.h"
static int install_combined_signed_height(uintptr_t engine) {
    static int installed;
    if (!install_combined_component_guard(engine) || !install_query_signed_height(engine)) return 0;
    if (!installed) {
        printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-distance-component-height-v1 installed=1 getter_sites=11 distance_functions=2 component_functions=1 query_blocks=1\n");
        fflush(stdout); installed=1;
    }
    return 1;
}
#endif
