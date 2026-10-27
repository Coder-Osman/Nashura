from pathlib import Path
import shutil
import subprocess

import pytest


def test_real_java_helper_restores_then_retires_only_its_own_references(tmp_path):
    java = shutil.which('java')
    javac = shutil.which('javac')
    if not java or not javac:
        pytest.skip('JDK required for engineering fixture; no native evidence claimed')
    sources = {
        'android/content/Context.java': '''
package android.content;
public abstract class Context {
    public abstract ContentResolver getContentResolver();
    public abstract String getPackageName();
}
''',
        'android/content/ContentResolver.java': '''
package android.content;
public class ContentResolver {}
''',
        'android/provider/Settings.java': '''
package android.provider;
public class Settings {
    public static class Secure {
        public static String getString(android.content.ContentResolver resolver, String key) {
            throw new UnsupportedOperationException("identity acquisition outside release fixture");
        }
    }
}
''',
        'android/content/SharedPreferences.java': '''
package android.content;
public interface SharedPreferences {
    String getString(String key, String fallback);
    boolean contains(String key);
    Editor edit();
    interface Editor { Editor putString(String key, String value); Editor remove(String key); boolean commit(); }
}
''',
        'android/util/Base64.java': '''
package android.util;
public class Base64 {
    public static final int NO_WRAP = 2;
    public static byte[] decode(String value, int flags) { throw new UnsupportedOperationException("unused PN fixture"); }
    public static String encodeToString(byte[] value, int flags) { throw new UnsupportedOperationException("unused codec fixture"); }
}
''',
        'org/json/JSONObject.java': '''
package org.json;
public class JSONObject {
    public JSONObject put(String key, Object value) { throw new UnsupportedOperationException("unused PN fixture"); }
}
''',
        'org/json/JSONArray.java': '''
package org.json;
public class JSONArray {
    public JSONArray put(Object value) { throw new UnsupportedOperationException("unused PN fixture"); }
}
''',
        'com/supercell/titan/GameApp.java': '''
package com.supercell.titan;
import android.content.SharedPreferences;
public class GameApp {
    public static GameApp q = new GameApp();
    public Store o = new Store();
    public Store p = new Store();
    public static class Store { public SharedPreferences d = new Preferences(); }
    public static class Preferences implements SharedPreferences {
        public int commits;
        public String value = "fixture";
        public String getString(String key, String fallback) { return value; }
        public boolean contains(String key) { return true; }
        public Editor edit() {
            return new Editor() {
                public Editor putString(String key, String replacement) { value = replacement; return this; }
                public Editor remove(String key) { value = null; return this; }
                public boolean commit() { ++commits; return false; }
            };
        }
    }
}
''',
        'Harness.java': '''
import android.content.SharedPreferences;
import com.supercell.titan.GameApp;
import java.lang.reflect.*;
import java.net.*;
import java.nio.file.*;
public class Harness {
    static Method command;
    static String call(String operation) throws Exception { return (String)command.invoke(null, operation); }
    static void require(boolean result) { if (!result) throw new AssertionError(); }
    static void rejected(String operation) throws Exception {
        try { call(operation); throw new AssertionError(); }
        catch (InvocationTargetException failure) { require(failure.getCause() instanceof IllegalStateException); }
    }
    public static void main(String[] args) throws Exception {
        URLClassLoader loader = new URLClassLoader(new URL[]{Paths.get(args[0]).toUri().toURL()}, Harness.class.getClassLoader());
        Class<?> helper = Class.forName("org.nashura.evidence.E21PreferenceObserver", true, loader);
        command = helper.getMethod("command", String.class);
        SharedPreferences first = GameApp.q.o.d, second = GameApp.q.p.d;
        rejected("release");
        require(call("install").equals("installed\\t2"));
        rejected("release");
        require(GameApp.q.o.d != first && GameApp.q.p.d != second);
        SharedPreferences.Editor editor = GameApp.q.o.d.edit();
        require(editor.putString("fixture", "updated") == editor);
        require(!editor.commit());
        require(((GameApp.Preferences)first).commits == 1);
        require(GameApp.q.o.d.getString("fixture", "").equals("updated"));
        require(call("snapshot").contains("commit:exit\\tfalse"));
        require(call("uninstall").equals("uninstalled\\t2"));
        require(GameApp.q.o.d == first && GameApp.q.p.d == second);
        GameApp.q.o.d = second;
        rejected("release");
        GameApp.q.o.d = first;
        require(call("release").equals("released\\t6"));
        for (String name : new String[]{"stores", "originals", "proxies"}) {
            Field field = helper.getDeclaredField(name); field.setAccessible(true);
            for (Object reference : (Object[])field.get(null)) require(reference == null);
        }
        Field backing = helper.getDeclaredField("backing"); backing.setAccessible(true);
        require(backing.get(null) == null);
        require(GameApp.q.o.d == first && GameApp.q.p.d == second);
        rejected("snapshot"); rejected("install"); rejected("release");
        loader.close();
        System.out.println("Java release engineering fixture passed; no GC or native claim");
    }
}
'''}
    files = []
    for name, text in sources.items():
        target = tmp_path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)
        files.append(str(target))
    classes, helper = tmp_path / 'classes', tmp_path / 'helper'
    subprocess.run([javac, '--release', '8', '-d', str(classes), *files], check=True, capture_output=True)
    source = Path(__file__).parents[1] / 'E21PreferenceObserver.java'
    subprocess.run([javac, '--release', '8', '-classpath', str(classes), '-d', str(helper), str(source)],
                   check=True, capture_output=True)
    result = subprocess.run([java, '-cp', str(classes), 'Harness', str(helper)], check=True,
                            capture_output=True, text=True)
    assert 'engineering fixture passed; no GC or native claim' in result.stdout
