package com.nashura.benchmark;
import android.app.Instrumentation;
import android.os.Bundle;
import android.system.Os;
import java.io.*;
import java.lang.reflect.*;
public final class BenchRunner extends Instrumentation {
    private Bundle options;
    private static native void configure(String key,String value);
    public void onCreate(Bundle args){options=args;start();}
    public void onStart(){
        if(android.os.Looper.myLooper()==null)android.os.Looper.prepare();
        Bundle result=new Bundle();
        String dir=getTargetContext().getFilesDir().getAbsolutePath();
        try {
            File log=new File(dir,options.getString("label","probe")+".log");
            FileOutputStream stream=new FileOutputStream(log);
            Os.dup2(stream.getFD(),1);Os.dup2(stream.getFD(),2);
            System.setOut(new PrintStream(stream,true));System.setErr(System.out);
            System.loadLibrary("nashura_bench_env");
            String mode=options.getString("mode","trace");
            configure("NASHURA_BATTLE_STEPS","10000");
            configure("NASHURA_SNAPSHOT_ADAPTER","1");
            configure("NASHURA_QUIET","1");
            configure("NASHURA_QUIET_SNAPSHOTS","1");
            configure("NASHURA_BENCHMARK_VALIDATE","1");
            configure("NASHURA_TASK_TIMEOUT_MS","120000");
            configure("NASHURA_MEMORY_SAMPLE","1");
            configure("NASHURA_RESET_REPEATS",options.getString("repeats","1"));
            if(mode.equals("trace"))configure("NASHURA_TRACE_SNAPSHOTS",dir+"/"+options.getString("label","probe")+".bin");
            System.out.println("NASHURA_X86_ANDROID_NATIVE_BRIDGE=libndk_translation.so");
            String base="/data/local/tmp/nashura-x86";
            System.load(base+"/libc++_shared.so");
            System.load(base+"/libscid_sdk.so");
            String[] args={base+"/libg.so",base+"/libart_engine_jni_probe.so",base+"/game.apk",dir+"/"+options.getString("label","probe")+"-data","callbacks","apk-context",base+"/libheadless_observer.so","native-create","native-destroy","native-active","game-state-action-step",base+"/replay.json","fec6da6425549e1c62ef3709a3517826b4000ee27f98566122d0966ec6e0d0f8"};
            Class.forName("ArtHeadlessCreateProbe").getMethod("main",String[].class).invoke(null,(Object)args);
            System.out.println("NASHURA_X86_ANDROID_PROBE_RETURN=1");
            result.putString("status","returned");finish(-1,result);
        } catch(Throwable t){t.printStackTrace();result.putString("failure",t.toString());finish(1,result);}
    }
}
