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
if [ -n "${NASHURA_GRAPHICS_INPUTS:-}" ]; then
    case "$NASHURA_GRAPHICS_INPUTS" in /*) ;; *) exit 74 ;; esac
    [ "$NASHURA_GRAPHICS_INPUTS" != / ] && [ ! -L "$NASHURA_GRAPHICS_INPUTS" ] || exit 74
    [ -d "$NASHURA_GRAPHICS_INPUTS" ] || exit 74
    for binary in surfaceflinger gpuservice; do
        [ -x "$NASHURA_GRAPHICS_INPUTS/$binary" ] || exit 74
        mkdir -p "$ART_BASE/system/bin"
        touch "$ART_BASE/system/bin/$binary"
        mount --bind "$NASHURA_GRAPHICS_INPUTS/$binary" "$ART_BASE/system/bin/$binary"
        mount -o remount,bind,ro "$ART_BASE/system/bin/$binary"
    done
    # The default linker namespace searches /system/lib64.  Keep the two
    # statsd dependencies explicit and read-only instead of relying on an
    # ambient APEX search path.
    for library in libstatssocket.so libstatspull.so; do
        [ -f "$NASHURA_GRAPHICS_INPUTS/$library" ] || exit 74
        mkdir -p "$ART_BASE/system/lib64"
        touch "$ART_BASE/system/lib64/$library"
        mount --bind "$NASHURA_GRAPHICS_INPUTS/$library" "$ART_BASE/system/lib64/$library"
        mount -o remount,bind,ro "$ART_BASE/system/lib64/$library"
    done
fi
if [ -n "${NASHURA_GRAPHICS_VENDOR_ROOT:-}" ]; then
    case "$NASHURA_GRAPHICS_VENDOR_ROOT" in /*) ;; *) exit 75 ;; esac
    [ "$NASHURA_GRAPHICS_VENDOR_ROOT" != / ] && [ ! -L "$NASHURA_GRAPHICS_VENDOR_ROOT" ] || exit 75
    [ -x "$NASHURA_GRAPHICS_VENDOR_ROOT/bin/hw/android.hardware.graphics.allocator@3.0-service" ] || exit 75
    [ -x "$NASHURA_GRAPHICS_VENDOR_ROOT/bin/hw/android.hardware.graphics.composer@2.3-service" ] || exit 75
    mount --bind "$NASHURA_GRAPHICS_VENDOR_ROOT" "$ART_BASE/vendor"
    mount -o remount,bind,ro "$ART_BASE/vendor"
    mkdir -p "$ART_BASE/dev/dri" "$ART_BASE/dev/dma_heap"
    mount -t tmpfs -o mode=0755 tmpfs "$ART_BASE/dev/dri"
    create_device_node() {
        node_path=$1
        device_source=$2
        device_gid=$3
        device_numbers=$(stat -c '%t %T' "$device_source") || exit 76
        set -- $device_numbers
        [ "$#" -eq 2 ] || exit 76
        major=$((0x$1))
        minor=$((0x$2))
        mknod "$node_path" c "$major" "$minor"
        chmod 0660 "$node_path"
        chown "0:$device_gid" "$node_path"
    }
    for device in card0 renderD128; do
        source="/dev/dri/$device"
        [ -c "$source" ] || exit 76
        if [ "$device" = renderD128 ]; then
            device_gid=1030
        else
            device_gid=1003
        fi
        create_device_node "$ART_BASE/dev/dri/$device" "$source" "$device_gid"
    done
    if [ -c /dev/dma_heap/system ]; then
        mount -t tmpfs -o mode=0755 tmpfs "$ART_BASE/dev/dma_heap"
        create_device_node "$ART_BASE/dev/dma_heap/system" /dev/dma_heap/system 1003
    fi
    mkdir -p "$ART_BASE/sys"
    mount --rbind /sys "$ART_BASE/sys"
    mount -o remount,bind,ro "$ART_BASE/sys"
fi
if [ -n "${NASHURA_APEX_PACKAGES:-}" ]; then
    [ "${NASHURA_ROOT_INIT_TRIAL:-0}" = 1 ] && [ "$(id -u)" = 0 ] || exit 71
    case "$NASHURA_APEX_PACKAGES" in /*) ;; *) exit 71 ;; esac
    case "${NASHURA_APEX_LOOP_DEVICES:-}" in /*) ;; *) exit 71 ;; esac
    [ "$NASHURA_APEX_PACKAGES" != / ] && [ ! -L "$NASHURA_APEX_PACKAGES" ] || exit 71
    [ -f "$NASHURA_APEX_PACKAGES/com.android.runtime.apex" ] || exit 71
    [ -d "$NASHURA_APEX_LOOP_DEVICES/block" ] && [ -c /dev/loop-control ] || exit 71
    mkdir -p "$ART_BASE/system/apex" "$ART_BASE/dev/block" "$ART_BASE/sys/block"
    mount --bind "$NASHURA_APEX_PACKAGES" "$ART_BASE/system/apex"
    mount -o remount,bind,ro "$ART_BASE/system/apex"
    mount --bind "$NASHURA_APEX_LOOP_DEVICES/block" "$ART_BASE/dev/block"
    mount -o remount,bind,ro "$ART_BASE/dev/block"
    touch "$ART_BASE/dev/loop-control"
    mount --bind /dev/loop-control "$ART_BASE/dev/loop-control"
    # Expose only loops leased for this trial. Occupied guest disks stay outside.
    for device in "$NASHURA_APEX_LOOP_DEVICES"/block/loop*; do
        [ -b "$device" ] || exit 71
        name=${device##*/}
        mkdir -p "$ART_BASE/sys/block/$name"
        mount --bind "/sys/block/$name" "$ART_BASE/sys/block/$name"
        mount -o remount,bind,ro "$ART_BASE/sys/block/$name"
        # Original apexd configures this one queue value after LOOP_SET_FD.
        path="$ART_BASE/sys/block/$name/queue/read_ahead_kb"
        mount --bind "/sys/block/$name/queue/read_ahead_kb" "$path"
        mount -o remount,bind,rw "$path"
    done
fi
ln -sfn /proc/self/fd "$ART_BASE/dev/fd"
ln -sfn /proc/self/fd/0 "$ART_BASE/dev/stdin"
ln -sfn /proc/self/fd/1 "$ART_BASE/dev/stdout"
ln -sfn /proc/self/fd/2 "$ART_BASE/dev/stderr"
if [ -n "${NASHURA_ART_LINKER_CONFIG:-}" ]; then
    case "$NASHURA_ART_LINKER_CONFIG" in /*) ;; *) exit 69 ;; esac
    [ -f "$NASHURA_ART_LINKER_CONFIG" ] || exit 69
    touch "$ART_BASE/linkerconfig/ld.config.txt"
    mount --bind "$NASHURA_ART_LINKER_CONFIG" "$ART_BASE/linkerconfig/com.android.art/ld.config.txt"
    mount --bind "$NASHURA_ART_LINKER_CONFIG" "$ART_BASE/linkerconfig/ld.config.txt"
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
    if [ "${NASHURA_RUN_HWSERVICEMANAGER:-0}" = 1 ]; then
        [ -x "$NASHURA_TOOLS_INPUTS/hwservicemanager" ] || exit 72
        [ -f "$ART_BASE/system/lib64/android.hidl.token@1.0.so" ] || exit 72
        touch "$ART_BASE/system/bin/hwservicemanager"
        mount --bind "$NASHURA_TOOLS_INPUTS/hwservicemanager" "$ART_BASE/system/bin/hwservicemanager"
        mount -o remount,bind,ro "$ART_BASE/system/bin/hwservicemanager"
    fi
    if [ "${NASHURA_RUN_SUSPEND_HAL:-0}" = 1 ]; then
        [ -x "$NASHURA_TOOLS_INPUTS/android.system.suspend@1.0-service" ] || exit 73
        mkdir -p "$ART_BASE/system/bin/hw"
        touch "$ART_BASE/system/bin/hw/android.system.suspend@1.0-service"
        mount --bind "$NASHURA_TOOLS_INPUTS/android.system.suspend@1.0-service" \
            "$ART_BASE/system/bin/hw/android.system.suspend@1.0-service"
        mount -o remount,bind,ro "$ART_BASE/system/bin/hw/android.system.suspend@1.0-service"
    fi
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
        if [ -n "${NASHURA_EXEC_GROUPS:-}" ]; then
            exec /nashura-tools/nashura_namespace_exec --user "$NASHURA_EXEC_UID" "${NASHURA_EXEC_GID:?}" \
                --groups "$NASHURA_EXEC_GROUPS" "$@"
        fi
        exec /nashura-tools/nashura_namespace_exec --user "$NASHURA_EXEC_UID" "${NASHURA_EXEC_GID:?}" "$@"
    fi
    exec /nashura-tools/nashura_namespace_exec "$@"
fi
exec /apex/com.android.runtime/bin/linker64 "$@"
