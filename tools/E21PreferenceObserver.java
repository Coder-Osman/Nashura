package org.nashura.evidence;

import android.content.SharedPreferences;
import java.lang.reflect.Field;
import java.lang.reflect.InvocationHandler;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.lang.reflect.Proxy;
import java.util.ArrayList;
import java.util.List;

public final class E21PreferenceObserver {
    private static final List<String> events = new ArrayList<>();
    private static final Object[] stores = new Object[2];
    private static final Object[] originals = new Object[2];
    private static final Object[] proxies = new Object[2];
    private static Field backing;
    private static boolean installed;
    private static long sequence;
    private static long editors;
    private static int dropped;

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
        if (installed || backing != null) throw new IllegalStateException("single install only");
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
        if (operation.equals("install")) return install();
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
        throw new IllegalArgumentException("unknown observer command");
    }
}
