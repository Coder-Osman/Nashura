#!/bin/sh
# Run inside: unshare -Ur -m sh art_namespace.sh /absolute/closure /absolute/program [args...]
# The caller provides extracted SDK files; this script does not create an Android OS.
set -eu
ulimit -c 0
[ "$#" -ge 2 ] || exit 64
ART_BASE=$1
shift
case "$ART_BASE" in /*) ;; *) exit 64 ;; esac
[ "$ART_BASE" != / ] && [ ! -L "$ART_BASE" ] || exit 64
[ -f "$ART_BASE/apex/com.android.runtime/bin/linker64" ] || exit 65
[ -f "$ART_BASE/linkerconfig/com.android.art/ld.config.txt" ] || exit 65
case "$1" in /*) ;; *) exit 64 ;; esac

mount --make-rprivate /
mount --bind "$ART_BASE" "$ART_BASE"
mkdir -p "$ART_BASE/proc" "$ART_BASE/oldroot" "$ART_BASE/dev" \
    "$ART_BASE/dev/__properties__" "$ART_BASE/data/dalvik-cache" \
    "$ART_BASE/data/sockets" "$ART_BASE/tmp"
mount --rbind /proc "$ART_BASE/proc"
if [ "${NASHURA_BIND_SELINUXFS:-0}" = 1 ]; then
    [ -f /sys/fs/selinux/status ] || exit 67
    [ "$(stat -f -c %t /sys/fs/selinux)" = f97cff8c ] || exit 67
    mkdir -p "$ART_BASE/sys/fs/selinux"
    mount --bind /sys/fs/selinux "$ART_BASE/sys/fs/selinux"
fi
for device in null zero random urandom; do
    touch "$ART_BASE/dev/$device"
    mount --bind "/dev/$device" "$ART_BASE/dev/$device"
done
for device in binder hwbinder vndbinder; do
    [ -c "/dev/binderfs/$device" ] || exit 66
    touch "$ART_BASE/dev/$device"
    mount --bind "/dev/binderfs/$device" "$ART_BASE/dev/$device"
done
if [ -n "${NASHURA_INIT_STATE:-}" ]; then
    # Original init needs a fresh property area and sockets for every attempt.
    # Keep these writable outputs separate from earlier ART evidence.
    case "$NASHURA_INIT_STATE" in /*) ;; *) exit 68 ;; esac
    case "${NASHURA_VENDOR_INPUTS:-}" in /*) ;; *) exit 68 ;; esac
    [ "$NASHURA_INIT_STATE" != / ] && [ ! -L "$NASHURA_INIT_STATE" ] || exit 68
    [ "$NASHURA_VENDOR_INPUTS" != / ] && [ ! -L "$NASHURA_VENDOR_INPUTS" ] || exit 68
    [ -d "$NASHURA_INIT_STATE/properties" ] && [ -d "$NASHURA_INIT_STATE/socket" ] || exit 68
    [ -f "$NASHURA_INIT_STATE/kmsg" ] || exit 68
    [ -f "$NASHURA_VENDOR_INPUTS/etc/selinux/plat_sepolicy_vers.txt" ] || exit 68
    mkdir -p "$ART_BASE/vendor" "$ART_BASE/dev/socket"
    touch "$ART_BASE/dev/kmsg"
    mount --bind "$NASHURA_INIT_STATE/properties" "$ART_BASE/dev/__properties__"
    mount --bind "$NASHURA_INIT_STATE/socket" "$ART_BASE/dev/socket"
    # A regular log sink captures KernelLogger output; it is not a kernel device.
    mount --bind "$NASHURA_INIT_STATE/kmsg" "$ART_BASE/dev/kmsg"
    mount --bind "$NASHURA_VENDOR_INPUTS" "$ART_BASE/vendor"
fi
ln -sfn /proc/self/fd "$ART_BASE/dev/fd"
ln -sfn /proc/self/fd/0 "$ART_BASE/dev/stdin"
ln -sfn /proc/self/fd/1 "$ART_BASE/dev/stdout"
ln -sfn /proc/self/fd/2 "$ART_BASE/dev/stderr"
if [ -n "${NASHURA_ART_LINKER_CONFIG:-}" ]; then
    case "$NASHURA_ART_LINKER_CONFIG" in /*) ;; *) exit 69 ;; esac
    [ -f "$NASHURA_ART_LINKER_CONFIG" ] || exit 69
    mount --bind "$NASHURA_ART_LINKER_CONFIG" "$ART_BASE/linkerconfig/com.android.art/ld.config.txt"
fi
if [ "${NASHURA_ROOT_INIT_TRIAL:-0}" = 1 ]; then
    [ "$(id -u)" = 0 ] && [ "${NASHURA_BIND_SELINUXFS:-0}" = 1 ] || exit 70
    case "${NASHURA_TOOLS_INPUTS:-}" in /*) ;; *) exit 70 ;; esac
    [ "$NASHURA_TOOLS_INPUTS" != / ] && [ -d "$NASHURA_TOOLS_INPUTS" ] || exit 70
    [ -x "$NASHURA_TOOLS_INPUTS/nashura_namespace_exec" ] || exit 70
    [ -x "$NASHURA_TOOLS_INPUTS/toolbox" ] || exit 70
    mkdir -p "$ART_BASE/dev/pts"
    mount -t devpts -o newinstance,ptmxmode=0666,mode=0620 devpts "$ART_BASE/dev/pts"
    ln -sfn pts/ptmx "$ART_BASE/dev/ptmx"
    mkdir -p "$ART_BASE/tmp/root-trial-sysctls"
    for knob in kernel/kptr_restrict vm/mmap_rnd_bits vm/mmap_rnd_compat_bits; do
        name=${knob##*/}
        touch "$ART_BASE/tmp/root-trial-sysctls/$name"
        mount --bind "/proc/sys/$knob" "$ART_BASE/tmp/root-trial-sysctls/$name"
    done
    mount --bind "$ART_BASE/proc/sys" "$ART_BASE/proc/sys"
    mount -o remount,bind,ro "$ART_BASE/proc/sys"
    for knob in kernel/kptr_restrict vm/mmap_rnd_bits vm/mmap_rnd_compat_bits; do
        name=${knob##*/}
        mount --bind "$ART_BASE/tmp/root-trial-sysctls/$name" "$ART_BASE/proc/sys/$knob"
        mount -o remount,bind,rw "$ART_BASE/proc/sys/$knob"
    done
    mount --bind "$ART_BASE/proc/sysrq-trigger" "$ART_BASE/proc/sysrq-trigger"
    mount -o remount,bind,ro "$ART_BASE/proc/sysrq-trigger"
    # libselinux requires an rw selinuxfs to use its stateless calculation APIs.
    # Keep policy/enforcement/boolean controls read-only, retaining those APIs.
    for node in load enforce checkreqprot disable commit_pending_bools policy booleans avc; do
        path="$ART_BASE/sys/fs/selinux/$node"
        if [ -e "$path" ]; then
            mount --bind "$path" "$path"
            mount -o remount,bind,ro "$path"
        fi
    done
    mkdir -p "$ART_BASE/nashura-tools"
    mount --bind "$NASHURA_TOOLS_INPUTS" "$ART_BASE/nashura-tools"
    mount -o remount,bind,ro "$ART_BASE/nashura-tools"
fi
/usr/sbin/pivot_root "$ART_BASE" "$ART_BASE/oldroot"
cd /
export ANDROID_ROOT=/system ANDROID_DATA=/data
export ANDROID_RUNTIME_ROOT=/apex/com.android.runtime ANDROID_ART_ROOT=/apex/com.android.art
export ANDROID_I18N_ROOT=/apex/com.android.i18n ANDROID_TZDATA_ROOT=/apex/com.android.tzdata
export ANDROID_SOCKET_DIR=/data/sockets
# This order is the boot image's recorded class path, including ICU before okhttp.
export BOOTCLASSPATH=/apex/com.android.art/javalib/core-oj.jar:/apex/com.android.art/javalib/core-libart.jar:/apex/com.android.art/javalib/core-icu4j.jar:/apex/com.android.art/javalib/okhttp.jar:/apex/com.android.art/javalib/bouncycastle.jar:/apex/com.android.art/javalib/apache-xml.jar
# Namespace links must own Bionic; a global library path creates duplicate TLS instances.
unset LD_LIBRARY_PATH LD_PRELOAD LD_DEBUG LD_DEBUG_OUTPUT
if [ "${NASHURA_ROOT_INIT_TRIAL:-0}" = 1 ]; then
    if [ -n "${NASHURA_EXEC_UID:-}" ]; then
        exec /nashura-tools/nashura_namespace_exec --user "$NASHURA_EXEC_UID" "${NASHURA_EXEC_GID:?}" "$@"
    fi
    exec /nashura-tools/nashura_namespace_exec "$@"
fi
exec /apex/com.android.runtime/bin/linker64 "$@"
