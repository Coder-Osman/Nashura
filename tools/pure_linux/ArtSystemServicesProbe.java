/** Run original system-service entry points and report their actual failure boundary. */
public final class ArtSystemServicesProbe {
    private static String stage = "arguments";

    public static void main(String[] args) {
        if (args.length != 1 || !(args[0].equals("system-server") || args[0].equals("display"))) {
            throw new IllegalArgumentException("Expected system-server or display");
        }
        try {
            if (args[0].equals("system-server")) {
                stage = "SystemServer.main";
                System.out.println("ART_SYSTEM_SERVER_ENTERING=true");
                Class.forName("com.android.server.SystemServer").getMethod("main", String[].class)
                    .invoke(null, (Object) new String[0]);
                System.out.println("ART_SYSTEM_SERVER_RETURNED=true");
            } else {
                stage = "android_servers.load";
                System.loadLibrary("android_servers");
                System.out.println("ART_ANDROID_SERVERS_LOADED=true");
                stage = "system.context";
                Class<?> looper = Class.forName("android.os.Looper");
                looper.getMethod("prepareMainLooper").invoke(null);
                Class<?> threadClass = Class.forName("android.app.ActivityThread");
                Object thread = threadClass.getMethod("systemMain").invoke(null);
                Object context = threadClass.getMethod("getSystemContext").invoke(thread);
                System.out.println("ART_SYSTEM_CONTEXT_PRESENT=" + (context != null));
                stage = "SystemServiceManager.construct";
                Class<?> managerClass = Class.forName("com.android.server.SystemServiceManager");
                java.lang.reflect.Constructor<?> constructor = managerClass
                    .getDeclaredConstructor(Class.forName("android.content.Context"));
                constructor.setAccessible(true);
                Object manager = constructor.newInstance(context);
                Class.forName("com.android.server.LocalServices")
                    .getMethod("addService", Class.class, Object.class).invoke(null, managerClass, manager);
                stage = "PowerManagerService.start";
                Object power = managerClass.getMethod("startService", String.class)
                    .invoke(manager, "com.android.server.power.PowerManagerService");
                System.out.println("ART_POWER_SERVICE_STARTED=" + (power != null));
                stage = "DisplayManagerService.start";
                Object display = managerClass.getMethod("startService", String.class)
                    .invoke(manager, "com.android.server.display.DisplayManagerService");
                System.out.println("ART_DISPLAY_SERVICE_STARTED=" + (display != null));
                stage = "display.binder";
                Object binder = Class.forName("android.os.ServiceManager")
                    .getMethod("checkService", String.class).invoke(null, "display");
                System.out.println("ART_DISPLAY_BINDER_PRESENT=" + (binder != null));
                if (binder != null) {
                    System.out.println("ART_DISPLAY_BINDER_DESCRIPTOR=" + Class.forName("android.os.IBinder")
                        .getMethod("getInterfaceDescriptor").invoke(binder));
                }
            }
            System.out.println("ART_SYSTEM_SERVICES_PROBE_RETURNED_LIMITED");
            System.exit(0);
        } catch (Throwable failure) {
            while (failure instanceof java.lang.reflect.InvocationTargetException) {
                failure = ((java.lang.reflect.InvocationTargetException) failure).getCause();
            }
            System.err.println("ART_SYSTEM_SERVICES_FAILURE_STAGE=" + stage);
            System.err.println("ART_SYSTEM_SERVICES_FAILURE=" + failure.getClass().getName());
            failure.printStackTrace();
            System.exit(3);
        }
    }
}
