#include "qemu-plugin.h"
#include <errno.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <unistd.h>

QEMU_PLUGIN_EXPORT int qemu_plugin_version = QEMU_PLUGIN_VERSION;

typedef struct {
    struct qemu_plugin_register *registers[4];
    GByteArray *buffer;
} RegisterState;

static const uint64_t offsets[] = {
    0x121B118, 0x121B17C, 0x121B188, 0x121B284,
    0x121B2E4, 0x121B350, 0x121B394, 0x121B3AC
};
static const char *register_names[] = {"pc", "x0", "x1", "x30"};
static GHashTable *cpus;
static GMutex lock;
static FILE *output;
static uint64_t first_base;
static uint64_t last_base;
static uint64_t sequence;
static uint64_t armed_base;
static const char *arm_path;

static bool target_address(uint64_t address, uint64_t base)
{
    for (unsigned int target_index = 0; target_index < 8; target_index++) {
        if (address == base + offsets[target_index]) {
            return true;
        }
    }
    return false;
}

static void fatal(const char *message)
{
    fprintf(output, "ERROR\t%s\n", message);
    fflush(output);
    _exit(92);
}

static void destroy_state(gpointer pointer)
{
    RegisterState *state = pointer;
    g_byte_array_unref(state->buffer);
    g_free(state);
}

static void init_cpu(qemu_plugin_id_t identifier, unsigned int cpu_index)
{
    RegisterState *state = g_new0(RegisterState, 1);
    GArray *descriptors = qemu_plugin_get_registers();
    state->buffer = g_byte_array_new();
    for (guint descriptor_index = 0; descriptor_index < descriptors->len; descriptor_index++) {
        qemu_plugin_reg_descriptor *descriptor = &g_array_index(descriptors,
            qemu_plugin_reg_descriptor, descriptor_index);
        for (unsigned int register_index = 0; register_index < 4; register_index++) {
            if (strcmp(descriptor->name, register_names[register_index]) == 0) {
                state->registers[register_index] = descriptor->handle;
            }
        }
    }
    g_array_free(descriptors, TRUE);
    g_mutex_lock(&lock);
    for (unsigned int register_index = 0; register_index < 4; register_index++) {
        if (state->registers[register_index] == NULL) {
            fatal("required_aarch64_register_absent");
        }
    }
    if (g_hash_table_contains(cpus, GUINT_TO_POINTER(cpu_index))) {
        fatal("duplicate_vcpu_initialization");
    }
    g_hash_table_insert(cpus, GUINT_TO_POINTER(cpu_index), state);
    g_mutex_unlock(&lock);
}

static void execute_entry(unsigned int cpu_index, void *userdata)
{
    uint64_t expected_pc = (uint64_t)(uintptr_t)userdata;
    uint64_t values[4] = {0};
    uint64_t base = __atomic_load_n(&armed_base, __ATOMIC_ACQUIRE);
    if (arm_path != NULL && base != 0 && !target_address(expected_pc, base)) {
        return;
    }
    g_mutex_lock(&lock);
    if (arm_path != NULL) {
        if (armed_base == 0) {
            FILE *request = fopen(arm_path, "rb");
            if (request == NULL) {
                if (errno != ENOENT) {
                    fatal("arm_request_open_failed");
                }
                g_mutex_unlock(&lock);
                return;
            }
            char text[18] = {0};
            size_t length = fread(text, 1, sizeof(text), request);
            bool valid = length == 17 && text[16] == '\n' && !ferror(request);
            fclose(request);
            for (unsigned int index = 0; index < 16; index++) {
                valid = valid && ((text[index] >= '0' && text[index] <= '9') ||
                    (text[index] >= 'a' && text[index] <= 'f'));
            }
            text[16] = '\0';
            uint64_t requested = strtoull(text, NULL, 16);
            if (!valid || requested == 0 || requested % 4096 || requested >= (UINT64_C(1) << 63)) {
                fatal("arm_request_base_differs");
            }
            if (fprintf(output, "ARM\t%016" PRIx64 "\n", requested) < 0 || fflush(output) != 0) {
                fatal("arm_output_failed");
            }
            __atomic_store_n(&armed_base, requested, __ATOMIC_RELEASE);
        }
        if (!target_address(expected_pc, armed_base)) {
            g_mutex_unlock(&lock);
            return;
        }
    }
    RegisterState *state = g_hash_table_lookup(cpus, GUINT_TO_POINTER(cpu_index));
    if (state == NULL) {
        fatal("vcpu_register_state_absent");
    }
    for (unsigned int register_index = 0; register_index < 4; register_index++) {
        g_byte_array_set_size(state->buffer, 0);
        int length = qemu_plugin_read_register(state->registers[register_index], state->buffer);
        if (length != 8 || state->buffer->len != 8) {
            fatal("short_aarch64_register_read");
        }
        for (unsigned int byte_index = 0; byte_index < 8; byte_index++) {
            values[register_index] |= (uint64_t)state->buffer->data[byte_index] << (8 * byte_index);
        }
    }
    if (sequence >= 1000000) {
        fatal("record_limit_exceeded");
    }
    if (fprintf(output, "E\t%" PRIu64 "\t%u\t%016" PRIx64 "\t%016" PRIx64
            "\t%016" PRIx64 "\t%016" PRIx64 "\t%016" PRIx64 "\n", sequence, cpu_index,
            expected_pc, values[0], values[1], values[2], values[3]) < 0 || fflush(output) != 0) {
        fatal("entry_output_failed");
    }
    sequence++;
    g_mutex_unlock(&lock);
}

static void exit_cpu(qemu_plugin_id_t identifier, unsigned int cpu_index)
{
    g_mutex_lock(&lock);
    if (!g_hash_table_remove(cpus, GUINT_TO_POINTER(cpu_index))) {
        fatal("exiting_vcpu_state_absent");
    }
    g_mutex_unlock(&lock);
}

static void translate(qemu_plugin_id_t identifier, struct qemu_plugin_tb *block)
{
    size_t count = qemu_plugin_tb_n_insns(block);
    for (size_t instruction_index = 0; instruction_index < count; instruction_index++) {
        struct qemu_plugin_insn *instruction = qemu_plugin_tb_get_insn(block, instruction_index);
        uint64_t address = qemu_plugin_insn_vaddr(instruction);
        if (arm_path == NULL && (address < first_base + offsets[0] || address > last_base + offsets[7])) {
            continue;
        }
        for (unsigned int target_index = 0; target_index < 8; target_index++) {
            uint64_t candidate_base = address - offsets[target_index];
            if (candidate_base % 4096 == 0 && (arm_path != NULL ||
                    (candidate_base >= first_base && candidate_base <= last_base))) {
                qemu_plugin_register_vcpu_insn_exec_cb(instruction, execute_entry,
                    QEMU_PLUGIN_CB_R_REGS, (void *)(uintptr_t)address);
                break;
            }
        }
    }
}

static void finish(qemu_plugin_id_t identifier, void *userdata)
{
    g_mutex_lock(&lock);
    if (fprintf(output, "END\t%" PRIu64 "\n", sequence) < 0 || fflush(output) != 0) {
        fatal("footer_output_failed");
    }
    if (fclose(output) != 0) {
        _exit(92);
    }
    g_hash_table_destroy(cpus);
    g_mutex_unlock(&lock);
}

static bool parse_address(const char *argument, const char *prefix, uint64_t *result)
{
    size_t length = strlen(prefix);
    if (strncmp(argument, prefix, length) != 0 || argument[length] == '\0') {
        return false;
    }
    char *end = NULL;
    errno = 0;
    uint64_t value = strtoull(argument + length, &end, 16);
    if (errno || *end != '\0' || value == 0 || value % 4096 || value >= (UINT64_C(1) << 63)) {
        return false;
    }
    *result = value;
    return true;
}

QEMU_PLUGIN_EXPORT int qemu_plugin_install(qemu_plugin_id_t identifier,
        const qemu_info_t *info, int argument_count, char **arguments)
{
    if (strcmp(info->target_name, "aarch64") != 0 || info->system_emulation) {
        return 1;
    }
    const char *output_path;
    if (argument_count == 2 && strncmp(arguments[0], "arm=/", 5) == 0
            && strncmp(arguments[1], "output=/", 8) == 0) {
        arm_path = g_strdup(arguments[0] + 4);
        output_path = arguments[1] + 7;
    } else if (argument_count == 3 && parse_address(arguments[0], "first=", &first_base)
            && parse_address(arguments[1], "last=", &last_base)
            && last_base >= first_base && last_base - first_base == 127 * 4096
            && strncmp(arguments[2], "output=/", 8) == 0) {
        output_path = arguments[2] + 7;
    } else {
        return 1;
    }
    output = fopen(output_path, "wx");
    if (output == NULL) {
        return 1;
    }
    int written = arm_path != NULL ? fprintf(output, "FairFinalPluginEntries/v3\n") :
        fprintf(output, "FairFinalPluginEntries/v2\t%016" PRIx64 "\t%016" PRIx64 "\n", first_base, last_base);
    if (written < 0 || fflush(output) != 0) {
        fclose(output);
        return 1;
    }
    cpus = g_hash_table_new_full(g_direct_hash, g_direct_equal, NULL, destroy_state);
    qemu_plugin_register_vcpu_init_cb(identifier, init_cpu);
    qemu_plugin_register_vcpu_exit_cb(identifier, exit_cpu);
    qemu_plugin_register_vcpu_tb_trans_cb(identifier, translate);
    qemu_plugin_register_atexit_cb(identifier, finish, NULL);
    return 0;
}
