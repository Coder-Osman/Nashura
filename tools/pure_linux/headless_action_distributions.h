/* Bounded diagnostic samples; enabled only with the existing native phases.
 * No sample allocation, clock read or output is added to quiet benchmarks. */
#ifndef NASHURA_HEADLESS_ACTION_DISTRIBUTIONS_H
#define NASHURA_HEADLESS_ACTION_DISTRIBUTIONS_H

#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

#define ACTION_SAMPLE_CAPACITY 4096
enum action_sample_phase { AS_PARSE, AS_APPEND, AS_MODE2, AS_MODE3 };
static const char *const action_sample_names[] = {
    "injected_parse", "injected_append", "dispatch_mode2", "dispatch_mode3"
};
struct action_sample {
    enum action_sample_phase phase;
    uint64_t duration_ns;
    uintptr_t target_offset;
    int32_t input_index, tick, result, queue_before, queue_after;
};
static struct action_sample *action_samples;
static unsigned action_sample_count;

static int action_samples_begin(int allowed) {
    const char *value = getenv("NASHURA_ACTION_DISTRIBUTIONS");
    if (!value || strcmp(value, "1")) return 1;
    if (!allowed || action_samples) return 0;
    action_samples = calloc(ACTION_SAMPLE_CAPACITY, sizeof(*action_samples));
    action_sample_count = 0;
    return action_samples != NULL;
}
static void action_sample_add(enum action_sample_phase phase, uint64_t duration_ns,
        uintptr_t target_offset, int32_t input_index, int32_t tick, int32_t result,
        int32_t queue_before, int32_t queue_after) {
    if (!action_samples) return;
    if (action_sample_count == ACTION_SAMPLE_CAPACITY) _exit(126);
    action_samples[action_sample_count++] = (struct action_sample){
        phase, duration_ns, target_offset, input_index, tick, result, queue_before, queue_after
    };
}
static void action_samples_report(void) {
    if (!action_samples) return;
    printf("HEADLESS_ACTION_DISTRIBUTION scope=diagnostic-only capacity=%u samples=%u overflow=0\n",
        ACTION_SAMPLE_CAPACITY, action_sample_count);
    for (unsigned i = 0; i < action_sample_count; ++i) {
        const struct action_sample *s = &action_samples[i];
        printf("HEADLESS_ACTION_SAMPLE scope=diagnostic-only sequence=%u phase=%s duration_ns=%llu target_offset=0x%llx input_index=%d tick=%d result=%d queue_before=%d queue_after=%d\n",
            i + 1, action_sample_names[s->phase], (unsigned long long)s->duration_ns,
            (unsigned long long)s->target_offset, s->input_index, s->tick, s->result,
            s->queue_before, s->queue_after);
    }
    fflush(stdout);
    free(action_samples);
    action_samples = NULL;
    action_sample_count = 0;
}
#endif
