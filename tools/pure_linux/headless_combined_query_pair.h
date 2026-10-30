/* Explicit composition. Does not include the inconclusive grid-clear unroll. */
#ifndef NASHURA_HEADLESS_COMBINED_QUERY_PAIR_H
#define NASHURA_HEADLESS_COMBINED_QUERY_PAIR_H
#include "headless_combined_hot_distance.h"
#include "headless_query_dimension_pair.h"
static int install_combined_query_pair(uintptr_t engine) {
    static int installed;
    if (!install_combined_hot_distance(engine) || !install_query_dimension_pair(engine)) return 0;
    if (!installed) {
        printf("HEADLESS_NATIVE_OPTIMIZATION name=hot-distance-query-v1 installed=1 getter_sites=11 distance_functions=2 query_blocks=1\n");
        fflush(stdout);
        installed = 1;
    }
    return 1;
}
#endif
