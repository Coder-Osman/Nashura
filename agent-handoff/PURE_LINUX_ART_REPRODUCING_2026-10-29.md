# ARM64 Linux ART/JNI 阶段复现

此流程复现独立运行时与 VM 绑定观察。需要本机已有 SDK/NDK、JDK、`debugfs`，以及
ARM64 Linux guest 的 binderfs、user/mount namespace。它不启动 Android 模拟器。
第三方镜像和游戏库由操作者本地提供，不包含在公开仓库。

## 1. 提取匹配 SDK 闭包

本次输入是 SDK `system-images/android-30/google_apis/arm64-v8a/system.img`，
build `RSR1.240422.006/12134477`。本文件为 GPT 磁盘镜像，super 位于 2097152，
其中 system 单一 linear extent 的绝对字节偏移为 3145728，长度 800317440。
这些偏移只适用于本次输入；提取物必须匹配 SHA：
`a19fab1d61b2041d6945d6733bc32f4f3d90986017470b39849695e1a4437c0b`。
实验核对了 liblp metadata header/table 的 SHA，再进行提取。
格式依据为 [Android 11 liblp metadata_format.h](https://android.googlesource.com/platform/system/core/+/refs/heads/android11-release/fs_mgr/liblp/include/liblp/metadata_format.h)。

以只读 `debugfs -R` 从提取的 system.ext4 获取 `/system/lib64`、`/system/etc`、
`/system/bin/app_process64`、`/system/build.prop`，以及 `/system/apex` 下：

| APEX ZIP | 闭包目录 | payload 内目录 |
|---|---|---|
| `com.android.runtime.apex` | `/apex/com.android.runtime` | `bin lib64` |
| `com.android.art.debug.apex` | `/apex/com.android.art` | `bin lib64 javalib etc` |
| `com.android.i18n.apex` | `/apex/com.android.i18n` | `etc` |
| `com.google.android.conscrypt.apex` | `/apex/com.android.conscrypt` | `lib64 javalib etc` |
| `com.google.android.os.statsd.apex` | `/apex/com.android.os.statsd` | `lib64 javalib` |

每个 APEX 是 ZIP；解开后，以只读 `debugfs` 对其 `apex_payload.img` 执行 `rdump`。
保留符号链接。闭包内增加 `/system/bin/linker64` 到
`/apex/com.android.runtime/bin/linker64` 的符号链接。`root` 是下文 `$ART_CLOSURE`。
原始提取阶段为 997 个文件/符号链接，归档 100589129 字节；清单 SHA 为
`260f137123b897e7cb032010bf9c32bca34a152158208997cbd0c0a55d74e0fa`。

## 2. 属性和 linker 配置

`art_property_info.cpp` 使用 Android 11 原生 serializer 构造单一 context 的最小
`property_info`。编译依赖来自 AOSP system/core 的
`property_service/libpropertyinfoserializer` 中 `property_info_serializer.cpp`、
`trie_builder.cpp`、`trie_serializer.cpp` 及其头文件；还需 libpropertyinfoparser 的
`property_info_parser.cpp`/头文件，以及 `base/strings.cpp`/`android-base/strings.h`。
源版本是 `refs/heads/android11-release`，本阶段记录各源码 SHA 以供核对。

```sh
# $AOSP_INCLUDE 下有 property_info_serializer/、property_info_parser/、android-base/。
c++ -std=c++17 -Wall -Wextra -Werror -include cstring -I"$AOSP_INCLUDE" \
  tools/pure_linux/art_property_info.cpp "$AOSP_SERIALIZER/property_info_serializer.cpp" \
  "$AOSP_SERIALIZER/trie_builder.cpp" "$AOSP_SERIALIZER/trie_serializer.cpp" \
  "$AOSP_PARSER/property_info_parser.cpp" "$AOSP_BASE/strings.cpp" -o "$PRIVATE/art-property-info"
mkdir -p "$ART_CLOSURE/dev/__properties__" "$ART_CLOSURE/linkerconfig/com.android.art"
"$PRIVATE/art-property-info" "$ART_CLOSURE/dev/__properties__/property_info"
python tools/pure_linux/prepare_art_config.py \
  --stock "$ART_CLOSURE/apex/com.android.art/etc/ld.config.txt" \
  --stock-sha256 76ffeacf54f846f64e011aa832bd41e7e8ae9e808e6d57e2a163527f144b8b64 \
  --output "$ART_CLOSURE/linkerconfig/com.android.art/ld.config.txt"
"$NDK_CLANG" -Wall -Wextra -Werror tools/pure_linux/art_property_bootstrap.c \
  -ldl -o "$PRIVATE/art-property-bootstrap"
```

生成 linker 配置 SHA 应为
`4bcfffbac2dc78dd4c76cff847f145d8044be5b7756554a4974ed7390c2207ce`。
将 bootstrap 和 SDK `build.prop` 放入闭包 `/tmp`。只在 namespace 内初始化属性：

```sh
unshare -Ur -m sh tools/pure_linux/art_namespace.sh "$ART_CLOSURE" \
  /tmp/art-property-bootstrap /tmp/build.prop
```

Linux 中 SELinux fsetxattr `EPERM` 会保留为 `PROPERTY_AREA_INIT=-1`；后续必须看到
`PROPERTY_ADDED 47 FAILED 0 ABI64 arm64-v8a`。该单一 context 属性区没有宣称
Android property service 或 SELinux 等价。不要在 guest 全局根直接运行 bootstrap。

## 3. JNI 生命周期

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -shared -fPIC -pthread \
  tools/pure_linux/art_jni_probe.c -o "$PRIVATE/libart_jni_probe.so"
"$JAVAC" --release 8 -d "$PRIVATE/classes" tools/pure_linux/ArtRuntimeProbe.java
"$D8" --min-api 30 --output "$PRIVATE/dex" "$PRIVATE/classes/ArtRuntimeProbe.class"
```

将 dex 和 probe 放入闭包 `/data/local/tmp/art-runtime.dex` 和
`/data/local/tmp/libart_jni_probe.so`。运行：

```sh
unshare -Ur -m sh tools/pure_linux/art_namespace.sh "$ART_CLOSURE" \
  /apex/com.android.art/bin/dalvikvm64 -Ximage:/apex/com.android.art/javalib/boot.art \
  -cp /data/local/tmp/art-runtime.dex ArtRuntimeProbe /data/local/tmp/libart_jni_probe.so
```

期望 exit 0、JNI mask 127、Java sum 49995000。脚本保留 `/oldroot`，共享 `/proc`、
PID 和网络 namespace。

## 4. 真实 libg 的 VM 绑定

```sh
"$NDK_CLANG" -Wall -Wextra -Werror -shared -fPIC \
  tools/pure_linux/art_engine_jni_probe.c -ldl -o "$PRIVATE/libart_engine_jni_probe.so"
"$JAVAC" --release 8 -d "$PRIVATE/classes" tools/pure_linux/ArtEngineLoadProbe.java
"$D8" --min-api 30 --output "$PRIVATE/engine-dex" "$PRIVATE/classes/ArtEngineLoadProbe.class"
```

闭包 `/data/local/tmp/libg-input` 仅放同版 APK 的 `libg.so`、`libscid_sdk.so`、
`libfmod.so`、`libfmodstudio.so`、`libsentry-android.so`、`libsentry.so`、
`libc++_shared.so`。SDK system 库由原闭包提供。Java fixture 在加载前检查完整
引擎 SHA，原生 observer 另检查 environment getter 的 16 字节入口。

把引擎 dex/probe 分别放到 `/data/local/tmp/engine-load.dex`、
`/data/local/tmp/libart_engine_jni_probe.so`。显式 boot classpath 顺序为
`core-oj core-libart core-icu4j okhttp bouncycastle apache-xml`（各在
`/apex/com.android.art/javalib/`，后缀 `.jar`），最后追加
`/apex/com.android.conscrypt/javalib/conscrypt.jar`。

```sh
unshare -Ur -m sh tools/pure_linux/art_namespace.sh "$ART_CLOSURE" \
  /apex/com.android.art/bin/dalvikvm64 -Ximage:/apex/com.android.art/javalib/boot.art \
  "-Xbootclasspath:$ART_BOOTCLASSPATH_WITH_CONSCRYPT" \
  -Djava.library.path=/data/local/tmp/libg-input -cp /data/local/tmp/engine-load.dex \
  ArtEngineLoadProbe /data/local/tmp/libg-input/libg.so /data/local/tmp/libart_engine_jni_probe.so
```

期望 mask 7、exit 0；必须同时保存 stderr 中 15 个 Java 类缺失记录。这仅观察
真实 VM 绑定，不进行 holder setup、JNI 完整注册或战斗状态验收。下一步使用实际
APK dex/framework 补齐类路径，再以相同输入身份接续 holder setup。
