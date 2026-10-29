/** Observe original framework initialization without supplying a synthetic Context. */
public final class ArtFrameworkContextProbe {
    public static void main(String[] args) throws Exception {
        if (args.length != 1 || !(args[0].equals("activity") || args[0].equals("system"))) {
            throw new IllegalArgumentException("Expected activity or system");
        }
        try {
            Class<?> looper = Class.forName("android.os.Looper");
            if (looper.getMethod("getMainLooper").invoke(null) == null) {
                looper.getMethod("prepareMainLooper").invoke(null);
            }
            if (args[0].equals("system")) {
                Class<?> threadClass = Class.forName("android.app.ActivityThread");
                Object thread = threadClass.getMethod("systemMain").invoke(null);
                System.out.println("ART_SYSTEM_MAIN_RETURNED=" + (thread != null));
                Object context = threadClass.getMethod("getSystemContext").invoke(thread);
                System.out.println("ART_SYSTEM_CONTEXT_PRESENT=" + (context != null));
                Class<?> contextClass = Class.forName("android.content.Context");
                System.out.println("ART_SYSTEM_CONTEXT_CLASS=" + context.getClass().getName());
                System.out.println("ART_SYSTEM_CONTEXT_PACKAGE=" + contextClass.getMethod("getPackageName").invoke(context));
                Object resources = contextClass.getMethod("getResources").invoke(context);
                System.out.println("ART_SYSTEM_CONTEXT_RESOURCES_PRESENT=" + (resources != null));
                Class<?> services = Class.forName("android.os.ServiceManager");
                Object manager = Class.forName("com.android.internal.os.BinderInternal")
                    .getMethod("getContextObject").invoke(null);
                System.out.println("ART_SERVICE_MANAGER_PRESENT=" + (manager != null));
                if (manager != null) {
                    Object descriptor = Class.forName("android.os.IBinder")
                        .getMethod("getInterfaceDescriptor").invoke(manager);
                    System.out.println("ART_SERVICE_MANAGER_DESCRIPTOR=" + descriptor);
                    System.out.println("ART_SERVICE_MANAGER_PING=" + Class.forName("android.os.IBinder")
                        .getMethod("pingBinder").invoke(manager));
                }
                Object display = services.getMethod("getService", String.class).invoke(null, "display");
                System.out.println("ART_DISPLAY_SERVICE_PRESENT=" + (display != null));
                Object packages = services.getMethod("getService", String.class).invoke(null, "package");
                System.out.println("ART_PACKAGE_SERVICE_PRESENT=" + (packages != null));
                Object gameContext = Class.forName("com.supercell.titan.GameApp")
                    .getMethod("getInstance").invoke(null);
                System.out.println("ART_GAME_CONTEXT_PRESENT=" + (gameContext != null));
            } else {
                Class<?> game = Class.forName("com.supercell.titan.GameApp");
                Object activity = game.getConstructor().newInstance();
                System.out.println("ART_GAME_ACTIVITY_CONSTRUCTED=" + (activity != null));
                System.out.flush();
                game.getMethod("onCreate", Class.forName("android.os.Bundle")).invoke(activity, new Object[] {null});
                Object context = game.getMethod("getInstance").invoke(null);
                System.out.println("ART_GAME_CONTEXT_PRESENT=" + (context != null));
            }
            System.out.println("ART_FRAMEWORK_CONTEXT_PROBE_RETURNED_LIMITED");
        } catch (java.lang.reflect.InvocationTargetException failure) {
            Throwable cause = failure.getCause();
            System.err.println("ART_FRAMEWORK_CONTEXT_FAILURE=" + cause.getClass().getName());
            cause.printStackTrace();
            System.exit(3);
        }
    }
}
