/* Explicit composition; all child guards remain. Install before battle only. */
#ifndef NASHURA_HEADLESS_COMBINED_GRID_CLEAR_H
#define NASHURA_HEADLESS_COMBINED_GRID_CLEAR_H
#include "headless_combined_hot_distance.h"
#include "headless_grid_clear_unroll.h"
static int install_combined_grid_clear(uintptr_t engine) {
    static int installed;
    if (!install_combined_hot_distance(engine) || !install_grid_clear_unroll(engine)) return 0;
    if (!installed) {
        printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-distance-grid-v1 installed=1 getter_sites=11 distance_functions=2 grid_functions=1\n");
        fflush(stdout);
        installed = 1;
    }
    return 1;
}
#endif
