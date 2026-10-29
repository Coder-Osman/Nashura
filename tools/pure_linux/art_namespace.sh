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
ln -sfn /proc/self/fd "$ART_BASE/dev/fd"
ln -sfn /proc/self/fd/0 "$ART_BASE/dev/stdin"
ln -sfn /proc/self/fd/1 "$ART_BASE/dev/stdout"
ln -sfn /proc/self/fd/2 "$ART_BASE/dev/stderr"
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
exec /apex/com.android.runtime/bin/linker64 "$@"
