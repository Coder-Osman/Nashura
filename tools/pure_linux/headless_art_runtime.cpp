/* Minimal process entry for the pinned Android11 ARM64 runtime.
 * Uses the real AndroidRuntime constructor, VM setup and native registration.
 * Calls HeadlessRuntimeEntry directly instead of RuntimeInit/AppRuntime, whose
 * onStarted starts a Binder thread pool before the Java main function.
 *
 * Build with matching AOSP Android11 libutils headers and original shared libs.
 * AndroidRuntime layout/virtual order follows android-11.0.0_r1:
 * frameworks/base/core/jni/include/android_runtime/AndroidRuntime.h.
 * This ABI declaration deliberately omits unrelated nonvirtual methods.
 * run_headless_match.py pins the runtime inputs before process launch.
 * Link the original ART sigchain.cc into this executable with exported signal
 * APIs; libart requires the real chain, not placeholder signal callbacks.
 */
#include <jni.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <utils/String8.h>
#include <utils/Vector.h>

namespace android {
class AndroidRuntime {
public:
    AndroidRuntime(char *arg_block, size_t arg_size);
    virtual ~AndroidRuntime();
    void addOption(const char *option, void *extra = nullptr);
    void start(const char *class_name, const Vector<String8> &options, bool zygote);
    virtual void onVmCreated(JNIEnv *env);
    virtual void onStarted() = 0;
    virtual void onZygoteInit() {}
    virtual void onExit(int) {}
private:
    Vector<JavaVMOption> options_;
    bool exit_without_cleanup_;
    char *const arg_block_;
    const size_t arg_size_;
};
static_assert(sizeof(AndroidRuntime) == 0x48, "Pinned Android11 ARM64 runtime ABI");
}

class HeadlessRuntime final : public android::AndroidRuntime {
public:
    HeadlessRuntime(char *arg_block, size_t arg_size)
        : AndroidRuntime(arg_block, arg_size) {}
    void onVmCreated(JNIEnv *) override {
        puts("HEADLESS_DIRECT_RUNTIME_VM_CREATED=1");
        fflush(stdout);
    }
    void onStarted() override {
        fputs("Unexpected RuntimeInit callback in direct headless entry\n", stderr);
        abort();
    }
};

int main(int argc, char **argv) {
    // --probe-args separates VM options from the existing probe's arguments.
    int separator = 1;
    while (separator < argc && strcmp(argv[separator], "--probe-args")) ++separator;
    if (separator == argc || separator + 1 == argc) return 64;
    HeadlessRuntime runtime(argv[0], strlen(argv[0]) + 1);
    for (int index = 1; index < separator; ++index) {
        if (argv[index][0] != '-') return 64;
        runtime.addOption(argv[index]);
    }
    android::Vector<android::String8> arguments;
    for (int index = separator + 1; index < argc; ++index) {
        arguments.add(android::String8(argv[index]));
    }
    puts("HEADLESS_DIRECT_RUNTIME_START=1");
    fflush(stdout);
    runtime.start("HeadlessRuntimeEntry", arguments, false);
    // The probe uses System.exit; a return here is not evidence of success.
    fputs("Headless entry returned without the probe exit marker\n", stderr);
    return 70;
}
