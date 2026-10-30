#!/bin/sh
# Run under unshare -Ur -m -n -i. The original app_process launcher currently
# uses Binder for startup/cleanup; this is not a battle dependency claim.
set -eu
ulimit -c 0
[ "$#" -ge 3 ] || exit 64
art_base=$1
linker_config=$2
shift 2
case "$art_base" in /*) ;; *) exit 64 ;; esac
case "$linker_config" in /*) ;; *) exit 64 ;; esac
case "$1" in /*) ;; *) exit 64 ;; esac
[ "$art_base" != / ] && [ ! -L "$art_base" ] || exit 64
[ -f "$art_base/apex/com.android.runtime/bin/linker64" ] || exit 65
[ -f "$art_base/linkerconfig/com.android.art/ld.config.txt" ] || exit 65
[ -f "$linker_config" ] || exit 65

mount --make-rprivate /
mount --bind "$art_base" "$art_base"
mkdir -p "$art_base/proc" "$art_base/oldroot" "$art_base/dev" \
    "$art_base/sys" "$art_base/data/dalvik-cache" "$art_base/data/sockets"
mount --rbind /proc "$art_base/proc"
# Preserve the existing local property data, without starting property service.
property_source="$art_base/tmp/headless-properties"
[ -f "$art_base/dev/__properties__/properties_serial" ] || exit 66
mkdir -p "$property_source"
mount --bind "$art_base/dev/__properties__" "$property_source"
mount -o remount,bind,ro "$property_source"
# Fresh mounts hide device files and SELinuxfs left in the extracted closure.
mount -t tmpfs -o mode=0755 tmpfs "$art_base/dev"
mount -t tmpfs -o mode=0555 tmpfs "$art_base/sys"
mkdir -p "$art_base/dev/__properties__" "$art_base/dev/socket"
mount --bind "$property_source" "$art_base/dev/__properties__"
for device in null zero random urandom; do
    touch "$art_base/dev/$device"
    mount --bind "/dev/$device" "$art_base/dev/$device"
done
for device in binder hwbinder; do
    [ -c "/dev/binderfs/$device" ] || exit 66
    touch "$art_base/dev/$device"
    mount --bind "/dev/binderfs/$device" "$art_base/dev/$device"
done
ln -s /proc/self/fd "$art_base/dev/fd"
ln -s /proc/self/fd/0 "$art_base/dev/stdin"
ln -s /proc/self/fd/1 "$art_base/dev/stdout"
ln -s /proc/self/fd/2 "$art_base/dev/stderr"
touch "$art_base/linkerconfig/ld.config.txt"
mount --bind "$linker_config" "$art_base/linkerconfig/com.android.art/ld.config.txt"
mount --bind "$linker_config" "$art_base/linkerconfig/ld.config.txt"
printf '%s\n' 'HEADLESS_NAMESPACE_BINDER_DEVICES=binder,hwbinder' \
    'HEADLESS_NAMESPACE_GRAPHICS_DEVICES=absent' 'HEADLESS_NAMESPACE_SELINUXFS=absent'
/usr/sbin/pivot_root "$art_base" "$art_base/oldroot"
cd /
export ANDROID_ROOT=/system ANDROID_DATA=/data
export ANDROID_RUNTIME_ROOT=/apex/com.android.runtime ANDROID_ART_ROOT=/apex/com.android.art
export ANDROID_I18N_ROOT=/apex/com.android.i18n ANDROID_TZDATA_ROOT=/apex/com.android.tzdata
export ANDROID_SOCKET_DIR=/data/sockets
export BOOTCLASSPATH=/apex/com.android.art/javalib/core-oj.jar:/apex/com.android.art/javalib/core-libart.jar:/apex/com.android.art/javalib/core-icu4j.jar:/apex/com.android.art/javalib/okhttp.jar:/apex/com.android.art/javalib/bouncycastle.jar:/apex/com.android.art/javalib/apache-xml.jar
unset LD_LIBRARY_PATH LD_PRELOAD LD_DEBUG LD_DEBUG_OUTPUT
exec /apex/com.android.runtime/bin/linker64 "$@"
