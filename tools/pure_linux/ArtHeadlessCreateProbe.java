import java.io.File;
import java.io.FileInputStream;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.security.MessageDigest;

/** Direct original JNI startup; no Activity.onCreate, display, or service bootstrap. */
public final class ArtHeadlessCreateProbe {
    private static native void prepareObservations();
    private static native void finishObservations();
    private static native void inspectState();
    private static native int runMainloopTask(boolean initialize);
    private static native int runContentBootstrapTask();
    private static native int runCatalogTask();
    private static native int runGameStateConstructorTask();
    private static native int runGameStateBattleTask(byte[] replayJson, int taskMode);
    private static native void stopForDebugger();
    private static void mark(String value) {
        System.out.println(value);
        System.out.flush();
    }

    private static void identity(String path, String expected) throws Exception {
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        try (FileInputStream stream = new FileInputStream(path)) {
            byte[] block = new byte[65536];
            int count;
            while ((count = stream.read(block)) != -1) digest.update(block, 0, count);
        }
        StringBuilder value = new StringBuilder();
        for (byte item : digest.digest()) value.append(String.format("%02x", item & 255));
        if (!value.toString().equals(expected)) throw new IllegalArgumentException("Input identity: " + path);
        mark("HEADLESS_INPUT_SHA256=" + expected);
    }

    private static byte[] readVerifiedReplay(String path, String expected) throws Exception {
        byte[] content = Files.readAllBytes(new File(path).toPath());
        if (content.length < 2 || content.length >= 64 * 1024) {
            throw new IllegalArgumentException("Replay JSON must contain 2..65535 bytes");
        }
        MessageDigest digest = MessageDigest.getInstance("SHA-256");
        StringBuilder value = new StringBuilder();
        for (byte item : digest.digest(content)) value.append(String.format("%02x", item & 255));
        if (!value.toString().equals(expected)) {
            throw new IllegalArgumentException("Replay JSON identity mismatch");
        }
        mark("HEADLESS_REPLAY_JSON_SHA256=" + expected);
        return content;
    }

    private static int serialBatches() {
        String value = System.getenv("NASHURA_SERIAL_BATCHES");
        int batches = value == null ? 1 : Integer.parseInt(value);
        if (batches < 1 || batches > 240) {
            throw new IllegalArgumentException("Serial batches must be between 1 and 240");
        }
        if (batches > 1 && (!"1".equals(System.getenv("NASHURA_BENCHMARK_MINIMAL"))
                || System.getenv("NASHURA_RESET_REPEATS") == null
                || System.getenv("NASHURA_TRACE_SNAPSHOTS") != null
                || System.getenv("NASHURA_NATIVE_PHASES") != null
                || System.getenv("NASHURA_THREAD_RESOURCES") != null)) {
            throw new IllegalArgumentException("Serial batches require quiet repeated warm loops");
        }
        return batches;
    }

    public static void main(String[] args) throws Exception {
        boolean battleTask = args.length >= 11 && (args[10].equals("game-state-load")
                || args[10].equals("game-state-load-gdb")
                || args[10].equals("game-state-step")
                || args[10].equals("game-state-step-gdb")
                || args[10].equals("game-state-action-load")
                || args[10].equals("game-state-action-step")
                || args[10].equals("game-state-snapshot")
                || args[10].equals("game-state-action-snapshot"));
        if ((args.length < 5 || args.length > 13)
                || !(args[4].equals("direct") || args[4].equals("callbacks"))
                || (args.length >= 6 && !args[5].equals("apk-context"))
                || (args.length >= 8 && !args[7].equals("native-create"))
                || (args.length >= 9 && !args[8].equals("native-destroy"))
                || (args.length >= 10 && !args[9].equals("native-active"))
                || (args.length >= 11 && !(args[10].equals("queue-observe")
                    || args[10].equals("native-init") || args[10].equals("native-init-gdb")
                    || args[10].equals("content-bootstrap") || args[10].equals("content-bootstrap-gdb")
                    || args[10].equals("catalog") || args[10].equals("catalog-gdb")
                    || args[10].equals("game-state-construct") || args[10].equals("game-state-construct-gdb")
                    || battleTask))
                || args.length == 12
                || (battleTask && args.length != 13)
                || (args.length == 13 && (!battleTask || !args[11].startsWith("/")
                    || !args[12].matches("[0-9a-f]{64}")))) {
            throw new IllegalArgumentException("Expected engine, VM observer, APK, new run root, "
                + "direct|callbacks, [apk-context, [headless observer, [native-create, [native-destroy, "
                + "[native-active, [task-mode, [replay-json-path replay-json-sha256]]]]]]]");
        }
        for (int index = 0; index < 4; index++) {
            if (!args[index].startsWith("/")) throw new IllegalArgumentException("Absolute paths required");
        }
        File run = new File(args[3]);
        if (!run.mkdir()) throw new IllegalArgumentException("Run root must be new");
        File data = new File(run, "data"), cache = new File(run, "cache");
        if (!data.mkdir() || !cache.mkdir()) throw new IllegalStateException("Run directories");
        identity(args[2], "a3708aa65005be7e8295a23ecbf61cb85d3951b64a41efbb38bc55eccb93a79f");
        identity("/data/local/tmp/current-game-classes.dex",
                 "c781c71b622aaee4c0c1b94f8ae0c9aa639e228d66fb929a0c9c4672a5f81aa6");
        try {
            Class<?> looper = Class.forName("android.os.Looper");
            if (looper.getMethod("getMainLooper").invoke(null) == null) {
                looper.getMethod("prepareMainLooper").invoke(null);
            }
            Class<?> assetsClass = Class.forName("android.content.res.AssetManager");
            Object assets = assetsClass.getConstructor().newInstance();
            int cookie = (Integer) assetsClass.getMethod("addAssetPath", String.class).invoke(assets, args[2]);
            if (cookie == 0) throw new IllegalStateException("APK asset path rejected");
            mark("HEADLESS_REAL_ASSET_MANAGER_COOKIE=" + cookie);
            Class<?> game = Class.forName("com.supercell.titan.GameApp");
            Object activity = game.getConstructor().newInstance();
            game.getField("q").set(null, activity);
            if (game.getMethod("getInstance").invoke(null) != activity) {
                throw new IllegalStateException("Original GameApp singleton identity");
            }
            mark("HEADLESS_ORIGINAL_GAMEAPP_SINGLETON=true");
            if (args.length >= 6) {
                Object context = Class.forName("HeadlessApkContext")
                    .getConstructor(String.class, String.class, assetsClass)
                    .newInstance(args[2], args[3], assets);
                // Activity.attachBaseContext also sets a UI autofill client.
                // This probe supplies the local Context without an Activity lifecycle.
                java.lang.reflect.Field base = Class.forName("android.content.ContextWrapper")
                    .getDeclaredField("mBase");
                base.setAccessible(true);
                base.set(activity, context);
                mark("HEADLESS_APK_BASE_CONTEXT_BOUND=true");
                Object version = Class.forName("com.supercell.titan.ApplicationUtilBase")
                    .getMethod("getAppVersion").invoke(null);
                if (!"15.535.13".equals(version)) throw new AssertionError("Original app version mismatch");
                mark("HEADLESS_ORIGINAL_GET_APP_VERSION=" + version);
            }
            if (args.length >= 7) {
                System.load(args[6]);
                prepareObservations();
            }
            System.load(new File(new File(args[0]).getParentFile(), "libfmod.so").getPath());
            ArtEngineLoadProbe.main(new String[] {args[0], args[1]});
            if (args.length >= 7) finishObservations();
            if (args[4].equals("callbacks")) {
                mark("HEADLESS_NATIVE_LIBRARIES_CALLBACK_ENTER");
                Class.forName("com.supercell.titan.TitanApplication")
                    .getMethod("nOnNativeLibrariesLoaded").invoke(null);
                mark("HEADLESS_APPLICATION_CALLBACK_ENTER");
                game.getMethod("nOnApplicationCreate").invoke(null);
                mark("HEADLESS_APPLICATION_CALLBACK_RETURN");
            }
            Method create = game.getMethod("createGameMain", assetsClass,
                String.class, String.class, String.class, long.class,
                int.class, int.class, int.class, float.class, float.class,
                int.class, String.class, Class.forName("android.app.Activity"));
            mark("HEADLESS_CREATE_ENTER mode=" + args[4] + " geometry=720x1280,dpi320,type2");
            Object result = create.invoke(null, assets, data.getPath(), cache.getPath(), cache.getPath(),
                cache.getUsableSpace(), 720, 1280, 320, 320.0f, 320.0f, 2, "(unknown)", activity);
            mark("HEADLESS_CREATE_RETURN_NULL=" + (result == null));
            if (result instanceof String) mark("HEADLESS_CREATE_RETURN_UTF16_LENGTH=" + ((String) result).length());
            if (args.length >= 8) {
                mark("HEADLESS_NATIVE_ON_CREATE_ENTER");
                game.getMethod("nOnCreate").invoke(null);
                mark("HEADLESS_NATIVE_ON_CREATE_RETURN");
                Thread.sleep(1000);
                inspectState();
                if (args.length >= 9) {
                    if (args.length >= 10) {
                        // nOnResume's observed getIntent().getFlags() JNI path
                        // requires a local launch Intent; no external launch data.
                        android.content.Intent intent = new android.content.Intent();
                        game.getMethod("setIntent", android.content.Intent.class).invoke(activity, intent);
                        mark("HEADLESS_FIXTURE_INTENT_FLAGS=" + intent.getFlags());
                        // Native callbacks observed in original onStart/onResume;
                        // skip the Java DisplayListener and Activity UI machinery.
                        mark("HEADLESS_NATIVE_ON_START_ENTER");
                        game.getMethod("nOnStart").invoke(null);
                        mark("HEADLESS_NATIVE_ON_START_RETURN");
                        mark("HEADLESS_NATIVE_ON_RESUME_ENTER");
                        game.getMethod("nOnResume").invoke(null);
                        mark("HEADLESS_NATIVE_ON_RESUME_RETURN");
                    }
                    if (args.length >= 11) {
                        int queued = runMainloopTask(false);
                        mark("HEADLESS_QUEUE_OBSERVATION_STATUS=" + queued);
                        if (queued != 1) throw new IllegalStateException("Mainloop task did not complete: " + queued);
                        if (args[10].startsWith("native-init")) {
                            if (args[10].equals("native-init-gdb")) stopForDebugger();
                            int initialized = runMainloopTask(true);
                            mark("HEADLESS_INIT_TASK_STATUS=" + initialized);
                            if (initialized != 1) throw new IllegalStateException("Mainloop init task did not complete: " + initialized);
                            inspectState();
                        } else if (args[10].startsWith("content-bootstrap") || args[10].startsWith("catalog")
                                || args[10].startsWith("game-state-construct") || battleTask) {
                            if (args[10].endsWith("-gdb") && !battleTask) stopForDebugger();
                            int completed = runContentBootstrapTask();
                            mark("HEADLESS_CONTENT_TASK_STATUS=" + completed);
                            if (completed != 1) throw new IllegalStateException("Content task did not complete: " + completed);
                            inspectState();
                            if (args[10].startsWith("catalog") || args[10].startsWith("game-state-construct")
                                    || battleTask) {
                                int loaded = runCatalogTask();
                                mark("HEADLESS_CATALOG_TASK_STATUS=" + loaded);
                                if (loaded != 1) throw new IllegalStateException("Catalog task did not complete: " + loaded);
                                inspectState();
                                if (args[10].startsWith("game-state-construct")) {
                                    int constructed = runGameStateConstructorTask();
                                    mark("HEADLESS_GAME_STATE_CONSTRUCTOR_TASK_STATUS=" + constructed);
                                    if (constructed != 1) throw new IllegalStateException("GameState task did not complete: " + constructed);
                                } else if (battleTask) {
                                    if (args[10].endsWith("-gdb")) stopForDebugger();
                                    byte[] replayJson = readVerifiedReplay(args[11], args[12]);
                                    int taskMode;
                                    switch (args[10]) {
                                    case "game-state-load":
                                    case "game-state-load-gdb":
                                        taskMode = 5;
                                        break;
                                    case "game-state-step":
                                    case "game-state-step-gdb":
                                        taskMode = 6;
                                        break;
                                    case "game-state-action-load":
                                        taskMode = 7;
                                        break;
                                    case "game-state-action-step":
                                        taskMode = 8;
                                        break;
                                    case "game-state-snapshot":
                                        taskMode = 9;
                                        break;
                                    case "game-state-action-snapshot":
                                        taskMode = 10;
                                        break;
                                    default:
                                        throw new AssertionError("Unknown battle task mode: " + args[10]);
                                    }
                                    int batches = serialBatches();
                                    for (int batch = 1; batch <= batches; batch++) {
                                        if (batches > 1) mark("HEADLESS_SERIAL_BATCH_BEGIN index=" + batch + " batches=" + batches);
                                        int battle = runGameStateBattleTask(replayJson, taskMode);
                                        mark("HEADLESS_GAME_STATE_BATTLE_TASK_STATUS=" + battle);
                                        if (battle != 1) throw new IllegalStateException("GameState battle task did not pass its runtime checks: " + battle);
                                        if (batches > 1) mark("HEADLESS_SERIAL_BATCH_END index=" + batch + " batches=" + batches + " status=1");
                                    }
                                }
                            }
                        }
                    }
                    Thread.sleep(9000);
                    mark("HEADLESS_STATE_AFTER_MS=10000");
                    inspectState();
                    if (args.length >= 10) {
                        mark("HEADLESS_NATIVE_ON_PAUSE_ENTER");
                        game.getMethod("nOnPause").invoke(null);
                        mark("HEADLESS_NATIVE_ON_PAUSE_RETURN");
                        mark("HEADLESS_NATIVE_ON_STOP_ENTER");
                        game.getMethod("nOnStop").invoke(null);
                        mark("HEADLESS_NATIVE_ON_STOP_RETURN");
                    }
                    // Original GameApp.onDestroy invokes this native callback
                    // (DEX 0x02e9fc). Do not invoke the UI Activity lifecycle.
                    mark("HEADLESS_NATIVE_ON_DESTROY_ENTER");
                    game.getMethod("nOnDestroy").invoke(null);
                    mark("HEADLESS_NATIVE_ON_DESTROY_RETURN");
                    inspectState();
                }
                mark("HEADLESS_NATIVE_ON_CREATE_OBSERVATION_END");
            }
            mark("HEADLESS_BATTLE_READY=UNVERIFIED");
            if (args.length >= 6) {
                java.util.concurrent.atomic.AtomicInteger rejected = (java.util.concurrent.atomic.AtomicInteger)
                    Class.forName("UnsupportedHeadlessPackageManager").getField("rejectedCalls").get(null);
                mark("HEADLESS_REJECTED_PM_CALLS=" + rejected.get());
                if (rejected.get() != 0) System.exit(4);
            }
        } catch (InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            mark("HEADLESS_FIRST_JAVA_FAILURE=" + cause.getClass().getName());
            cause.printStackTrace();
            System.exit(3);
        }
    }
}
