/* Explicit composition; no inconclusive query/grid/getter extension. */
#ifndef NASHURA_HEADLESS_COMBINED_COMPONENT_GUARD_H
#define NASHURA_HEADLESS_COMBINED_COMPONENT_GUARD_H
#include "headless_combined_hot_distance.h"
#include "headless_component_guard.h"
static int install_combined_component_guard(uintptr_t engine) {
    static int installed;
    if (!install_combined_hot_distance(engine) || !install_component_guard(engine,1)) return 0;
    if (!installed) {
        printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-distance-component-v1 installed=1 getter_sites=11 distance_functions=2 component_functions=1\n");
        fflush(stdout); installed=1;
    }
    return 1;
}
#endif
