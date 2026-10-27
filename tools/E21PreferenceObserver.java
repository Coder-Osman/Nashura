package org.nashura.evidence;

import android.content.SharedPreferences;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;
import android.util.Base64;
import javax.crypto.Cipher;
import org.json.JSONArray;
import org.json.JSONObject;

public final class E21PreferenceObserver {
    private static final List<String> events = new ArrayList<>();
    private static final Object[] stores = new Object[2];
    private static final Object[] originals = new Object[2];
    private static final Object[] proxies = new Object[2];
    private static Field backing;
    private static boolean installed;
    private static boolean released;
    private static long sequence;
    private static long editors;
    private static int dropped;
    private static String[] pnKeys;
    private static String[][] pnSaved;
    private static Method pnReader;
    private static Method pnWriter;
    private static int pnCommands;
    private static int codecCommands;
    private static int bridgeCommands;
    private static int derivationCommands;

    private static String bridgeCommand(String operation) throws Exception {
        if (!installed || ++bridgeCommands > 4096) {
            throw new IllegalStateException("bounded installed PN backend required");
        }
        if (operation.equals("bridge-backup")) {
            ClassLoader parent = E21PreferenceObserver.class.getClassLoader().getParent();
            Class<?> gameType = Class.forName("com.supercell.titan.GameApp", false, parent);
            Object game = gameType.getField("q").get(null);
            if (game == null) throw new IllegalStateException("original GameApp missing");
            gameType.getMethod("requestBackup").invoke(game);
            return "backup-method-returned; delivery-unknown";
        }
        String[] parts = operation.split(":", -1);
        if (parts.length < 2 || !(parts[1].equals("0") || parts[1].equals("1"))) {
            throw new IllegalArgumentException("explicit PN backend store required");
        }
        int index = Integer.parseInt(parts[1]);
        if (!stores[index].getClass().getName().equals("c.a")) {
            throw new IllegalStateException("unverified PN backend type");
        }
        if (parts[0].equals("bridge-read") && parts.length == 2) {
            Method reader = stores[index].getClass().getMethod("j", String.class);
            String result = (String)reader.invoke(stores[index], "PN_POPUP");
            if (result == null || result.indexOf('\0') >= 0 || result.getBytes("UTF-8").length > 4095) {
                throw new IllegalStateException("PN backend result outside contract");
            }
            return Base64.encodeToString(result.getBytes("UTF-8"), Base64.NO_WRAP);
        }
        if (parts[0].equals("bridge-write") && parts.length == 3 && parts[2].length() <= 5500) {
            byte[] bytes = Base64.decode(parts[2], Base64.NO_WRAP);
            String value = new String(bytes, "UTF-8");
            if (bytes.length > 4095 || value.indexOf('\0') >= 0 ||
                    !java.util.Arrays.equals(bytes, value.getBytes("UTF-8"))) {
                throw new IllegalArgumentException("bounded valid UTF-8 PN text required");
            }
            Method writer = stores[index].getClass().getMethod("n", String.class, String.class);
            writer.invoke(stores[index], "PN_POPUP", value);
            return "writer-method-returned; commit-observe-separately";
        }
        throw new IllegalArgumentException("unknown PN backend command");
    }

    private static String codecCommand(String operation) throws Exception {
        if (!installed || pnSaved == null || ++codecCommands > 128) {
            throw new IllegalStateException("bounded captured PN codec experiment required");
        }
        if (operation.equals("codec-info")) {
            JSONArray result = new JSONArray();
            for (int index = 0; index < 2; ++index) {
                JSONObject row = new JSONObject().put("store", index);
                for (String name : new String[]{"a", "b", "c"}) {
                    Cipher cipher = (Cipher)stores[index].getClass().getField(name).get(stores[index]);
                    byte[] iv = cipher.getIV();
                    row.put(name, new JSONObject().put("algorithm", cipher.getAlgorithm())
                        .put("provider", cipher.getProvider().getName())
                        .put("iv", iv == null ? "" : Base64.encodeToString(iv, Base64.NO_WRAP)));
                }
                row.put("charset", java.nio.charset.Charset.defaultCharset().name());
                result.put(row);
            }
            return result.toString();
        }
        String[] parts = operation.split(":", -1);
        if (parts.length < 2 || !(parts[1].equals("0") || parts[1].equals("1"))) {
            throw new IllegalArgumentException("explicit PN store required");
        }
        int index = Integer.parseInt(parts[1]);
        if (parts[0].equals("codec-state") && parts.length == 2) {
            SharedPreferences original = (SharedPreferences)originals[index];
            return new JSONObject().put("encoded", original.getString(pnKeys[index], null)).toString();
        }
        if (parts[0].equals("codec-read") && parts.length == 2) {
            String value = (String)pnReader.invoke(stores[index], "PN_POPUP");
            return Base64.encodeToString(value.getBytes("UTF-8"), Base64.NO_WRAP);
        }
        if (parts[0].equals("codec-raw") && parts.length == 3 && parts[2].length() <= 8192) {
            String value = new String(Base64.decode(parts[2], Base64.NO_WRAP), "US-ASCII");
            if (value.length() > 5500) throw new IllegalArgumentException("bounded raw ciphertext required");
            if (!((SharedPreferences)originals[index]).edit().putString(pnKeys[index], value).commit()) {
                throw new IllegalStateException("raw PN setup commit failed");
            }
            return "codec-raw-set";
        }
        if (parts[0].equals("codec-encode") && parts.length == 4 &&
                (parts[2].equals("key") || parts[2].equals("value")) && parts[3].length() <= 5500) {
            String value = new String(Base64.decode(parts[3], Base64.NO_WRAP), "UTF-8");
            if (value.getBytes("UTF-8").length > 4095) throw new IllegalArgumentException("bounded codec text required");
            Method encoder = stores[index].getClass().getDeclaredMethod("h", String.class, Cipher.class);
            encoder.setAccessible(true);
            Cipher cipher = (Cipher)stores[index].getClass().getField(parts[2].equals("key") ? "c" : "a").get(stores[index]);
            return (String)encoder.invoke(null, value, cipher);
        }
        throw new IllegalArgumentException("unknown bounded codec command");
    }

    private static String pnCommand(String operation) throws Exception {
        if (!installed || ++pnCommands > 80) throw new IllegalStateException("bounded installed PN matrix required");
        if (operation.equals("pn-begin")) {
            if (pnSaved != null) throw new IllegalStateException("PN capture already active");
            Class<?> type = stores[0].getClass();
            if (!type.getName().equals("c.a")) throw new IllegalStateException("unverified PN store type");
            pnReader = type.getMethod("j", String.class);
            pnWriter = type.getMethod("n", String.class, String.class);
            Method encoder = type.getDeclaredMethod("h", String.class, Cipher.class);
            encoder.setAccessible(true);
            Field cipher = type.getField("c");
            String[] capturedKeys = new String[2];
            String[][] capturedValues = new String[2][2];
            for (int index = 0; index < 2; ++index) {
                capturedKeys[index] = (String)encoder.invoke(null, "PN_POPUP", (Cipher)cipher.get(stores[index]));
                if (capturedKeys[index] == null || capturedKeys[index].equals("PN_POPUP")) {
                    throw new IllegalStateException("encoded PN key unavailable");
                }
                SharedPreferences original = (SharedPreferences)originals[index];
                capturedValues[index][0] = original.getString("PN_POPUP", null);
                capturedValues[index][1] = original.getString(capturedKeys[index], null);
            }
            pnKeys = capturedKeys;
            pnSaved = capturedValues;
            return "pn-captured\t2";
        }
        if (pnSaved == null) throw new IllegalStateException("PN state not captured");
        if (operation.startsWith("pn-set:")) {
            String[] values = operation.split(":", -1);
            if (values.length != 3) throw new IllegalArgumentException("PN pair required");
            String[] decoded = new String[2];
            for (int index = 0; index < 2; ++index) {
                decoded[index] = new String(Base64.decode(values[index + 1], Base64.NO_WRAP), "UTF-8");
                if (decoded[index].indexOf('\0') >= 0 || decoded[index].getBytes("UTF-8").length > 4095) {
                    throw new IllegalArgumentException("PN text out of bounds");
                }
            }
            for (int index = 0; index < 2; ++index) pnWriter.invoke(stores[index], "PN_POPUP", decoded[index]);
            return "pn-set\t2";
        }
        if (operation.equals("pn-state")) {
            JSONArray result = new JSONArray();
            for (int index = 0; index < 2; ++index) {
                SharedPreferences original = (SharedPreferences)originals[index];
                result.put(new JSONObject().put("store", index == 0 ? "o" : "p")
                    .put("value", pnReader.invoke(stores[index], "PN_POPUP"))
                    .put("plain", original.getString("PN_POPUP", null))
                    .put("encoded", original.getString(pnKeys[index], null)));
            }
            return result.toString();
        }
        if (operation.equals("pn-restore")) {
            for (int index = 0; index < 2; ++index) {
                SharedPreferences original = (SharedPreferences)originals[index];
                SharedPreferences.Editor editor = original.edit();
                String[] keys = {"PN_POPUP", pnKeys[index]};
                for (int key = 0; key < 2; ++key) {
                    if (pnSaved[index][key] == null) editor.remove(keys[key]);
                    else editor.putString(keys[key], pnSaved[index][key]);
                }
                if (!editor.commit()) throw new IllegalStateException("PN restoration commit failed");
                for (int key = 0; key < 2; ++key) {
                    String restored = original.getString(keys[key], null);
                    if (pnSaved[index][key] == null ? restored != null : !pnSaved[index][key].equals(restored)) {
                        throw new IllegalStateException("PN raw entry restoration mismatch");
                    }
                }
            }
            pnSaved = null;
            pnKeys = null;
            pnReader = null;
            pnWriter = null;
            return "pn-restored\t2";
        }
        throw new IllegalArgumentException("unknown PN matrix command");
    }

    private static synchronized void record(String store, long editor, String operation, String result) {
        if (events.size() >= 20000) { ++dropped; return; }
        events.add((++sequence) + "\t" + Thread.currentThread().getId() + "\t"
                + System.nanoTime() + "\t" + store + "\t" + editor + "\t" + operation + "\t" + result);
    }

    private static synchronized long nextEditor() { return ++editors; }

    private static Object wrap(Object target, Class<?> contract, String store, long editor) {
        return Proxy.newProxyInstance(E21PreferenceObserver.class.getClassLoader(), new Class<?>[]{contract},
                new InvocationHandler() {
                    public Object invoke(Object proxy, Method method, Object[] arguments) throws Throwable {
                        String name = method.getName();
                        record(store, editor, name + ":enter", "forward");
                        try {
                            Object result = method.invoke(target, arguments);
                            if (name.equals("edit")) {
                                result = wrap(result, SharedPreferences.Editor.class, store, nextEditor());
                            } else if (result == target) {
                                result = proxy;
                            }
                            record(store, editor, name + ":exit", name.equals("commit")
                                    ? String.valueOf(result) : "returned");
                            return result;
                        } catch (InvocationTargetException failure) {
                            Throwable original = failure.getCause();
                            record(store, editor, name + ":throw", original.getClass().getName());
                            throw original;
                        } catch (Throwable failure) {
                            record(store, editor, name + ":observer_error", failure.getClass().getName());
                            throw failure;
                        }
                    }
                });
    }

    private static String install() throws Exception {
        if (installed || backing != null || released) throw new IllegalStateException("single install only");
        ClassLoader parent = E21PreferenceObserver.class.getClassLoader().getParent();
        Class<?> gameType = Class.forName("com.supercell.titan.GameApp", false, parent);
        Object game = gameType.getField("q").get(null);
        if (game == null) throw new IllegalStateException("original GameApp missing");
        String[] names = {"o", "p"};
        for (int index = 0; index < 2; ++index) {
            stores[index] = gameType.getField(names[index]).get(game);
            if (stores[index] == null) throw new IllegalStateException("original store missing");
        }
        if (stores[0] == stores[1]) throw new IllegalStateException("store identities alias");
        backing = stores[0].getClass().getField("d");
        backing.setAccessible(true);
        for (int index = 0; index < 2; ++index) {
            originals[index] = backing.get(stores[index]);
            if (!(originals[index] instanceof SharedPreferences)) {
                throw new IllegalStateException("backing store is not SharedPreferences");
            }
            proxies[index] = wrap(originals[index], SharedPreferences.class, names[index], 0);
        }
        try {
            for (int index = 0; index < 2; ++index) {
                backing.set(stores[index], proxies[index]);
                if (backing.get(stores[index]) != proxies[index]) throw new IllegalStateException("install mismatch");
                record(names[index], 0, "install", originals[index].getClass().getName());
            }
            installed = true;
        } catch (Exception failure) {
            for (int index = 0; index < 2; ++index) backing.set(stores[index], originals[index]);
            throw failure;
        }
        return "installed\t2";
    }

    public static synchronized String command(String operation) throws Exception {
        if (released) throw new IllegalStateException("observer references retired");
        if (operation.equals("install")) return install();
        if (operation.equals("derivation-source")) {
            if (!installed || ++derivationCommands > 1) throw new IllegalStateException("single installed source query required");
            ClassLoader parent = E21PreferenceObserver.class.getClassLoader().getParent();
            Class<?> type = Class.forName("com.supercell.titan.GameApp", false, parent);
            Object game = type.getField("q").get(null);
            if (!(game instanceof android.content.Context)) throw new IllegalStateException("original app context missing");
            android.content.Context context = (android.content.Context)game;
            String identity = android.provider.Settings.Secure.getString(context.getContentResolver(), "android_id");
            if (identity == null || !identity.matches("[0-9a-f]{16}")) {
                throw new IllegalStateException("uncovered app identity; no default or fallback synthesized");
            }
            return new JSONObject().put("source", "original-app-context Settings.Secure")
                .put("identity", identity).put("package", context.getPackageName()).toString();
        }
        if (operation.startsWith("bridge-")) return bridgeCommand(operation);
        if (operation.startsWith("codec-")) return codecCommand(operation);
        if (operation.startsWith("pn-")) return pnCommand(operation);
        if (operation.equals("read-stores")) {
            if (!installed) throw new IllegalStateException("not installed");
            Method reader = stores[0].getClass().getMethod("j", String.class);
            return reader.invoke(stores[0], "PN_POPUP") + "\n" + reader.invoke(stores[1], "PN_POPUP");
        }
        if (operation.equals("files")) {
            if (!installed) throw new IllegalStateException("not installed");
            StringBuilder result = new StringBuilder();
            for (Object original : originals) {
                Field file = original.getClass().getDeclaredField("mFile");
                file.setAccessible(true);
                Object path = file.get(original);
                if (!(path instanceof java.io.File)) throw new IllegalStateException("backing file unknown");
                result.append(((java.io.File)path).getAbsolutePath()).append('\n');
            }
            return result.toString();
        }
        if (operation.equals("snapshot")) {
            StringBuilder result = new StringBuilder("sequence\tthread\tnanoseconds\tstore\teditor\toperation\tresult\n");
            for (String event : events) result.append(event).append('\n');
            return result.append("dropped\t").append(dropped).append('\n').toString();
        }
        if (operation.equals("uninstall")) {
            if (!installed) throw new IllegalStateException("not installed");
            if (pnSaved != null) throw new IllegalStateException("restore PN capture before uninstall");
            for (int index = 0; index < 2; ++index) {
                if (backing.get(stores[index]) != proxies[index]) throw new IllegalStateException("store changed externally");
            }
            for (int index = 0; index < 2; ++index) {
                backing.set(stores[index], originals[index]);
                if (backing.get(stores[index]) != originals[index]) throw new IllegalStateException("restore mismatch");
                record(index == 0 ? "o" : "p", 0, "uninstall", "restored");
            }
            installed = false;
            return "uninstalled\t2";
        }
        if (operation.equals("release")) {
            if (installed || backing == null) throw new IllegalStateException("restore stores before release");
            for (int index = 0; index < 2; ++index) {
                if (backing.get(stores[index]) != originals[index]) throw new IllegalStateException("restored store changed");
            }
            int cleared = 0;
            for (int index = 0; index < 2; ++index) {
                if (stores[index] != null) ++cleared;
                if (originals[index] != null) ++cleared;
                if (proxies[index] != null) ++cleared;
                stores[index] = null;
                originals[index] = null;
                proxies[index] = null;
            }
            backing = null;
            released = true;
            return "released\t" + cleared;
        }
        throw new IllegalArgumentException("unknown observer command");
    }
}
