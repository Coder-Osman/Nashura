/* Explicit opt-in composition. Both child installers retain their exact guards.
 * This does not include component-getters-v1 or spatial-y-bound-v1. A failed
 * child aborts the disposable process before battle; never retry partial work.
 */
#ifndef NASHURA_HEADLESS_COMBINED_HOT_DISTANCE_H
#define NASHURA_HEADLESS_COMBINED_HOT_DISTANCE_H
#include "headless_hot_getters.h"
#include "headless_saturated_distance.h"

static int install_combined_hot_distance(uintptr_t engine) {
    static int installed;
    if (!install_hot_getters(engine) || !install_saturated_distance(engine)) return 0;
    if (!installed) {
        printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-distance-v1 installed=1 getter_sites=11 distance_functions=2\n");
        fflush(stdout);
        installed = 1;
    }
    return 1;
}
#endif
