/** Run original system-service entry points and report their actual failure boundary. */
public final class ArtSystemServicesProbe {
    private static String stage = "arguments";

    private static void report(String message) {
        System.out.println(message);
        System.out.flush();
    }

    private static void reportError(String message) {
        System.err.println(message);
        System.err.flush();
    }

    private static void reportService(String prefix, Object service) {
        report(prefix + "_PRESENT=" + (service != null));
        if (service != null) {
            report(prefix + "_CLASS=" + service.getClass().getName());
        }
    }

    private static void reportFetcherDetails(Object fetcher, Object context) throws Exception {
        if (fetcher == null) {
            return;
        }
        Class<?> type = fetcher.getClass();
        report("ART_POWER_FETCHER_SUPER=" + type.getSuperclass().getName());
        java.lang.reflect.Method getServiceMethod = null;
        java.lang.reflect.Method createServiceMethod = null;
        for (Class<?> current = type; current != null; current = current.getSuperclass()) {
            for (java.lang.reflect.Field field : current.getDeclaredFields()) {
                if (field.getName().equals("mCacheIndex")) {
                    field.setAccessible(true);
                    Object cacheIndex = field.get(fetcher);
                    report("ART_POWER_FETCHER_CACHE_INDEX=" + cacheIndex);
                    try {
                        java.lang.reflect.Field cacheField = context.getClass().getDeclaredField("mServiceCache");
                        cacheField.setAccessible(true);
                        Object[] cache = (Object[]) cacheField.get(context);
                        int index = ((Number) cacheIndex).intValue();
                        report("ART_POWER_CACHE_SLOT_IN_RANGE=" + (index >= 0 && index < cache.length));
                        report("ART_POWER_CACHE_SLOT_PRESENT=" +
                            (index >= 0 && index < cache.length && cache[index] != null));
                        java.lang.reflect.Field statesField = context.getClass()
                            .getDeclaredField("mServiceInitializationStateArray");
                        statesField.setAccessible(true);
                        int[] states = (int[]) statesField.get(context);
                        report("ART_POWER_CACHE_STATE_LENGTH=" + states.length);
                        report("ART_POWER_CACHE_STATE_BEFORE=" + states[index]);
                        for (java.lang.reflect.Field stateField : current.getDeclaredFields()) {
                            if (java.lang.reflect.Modifier.isStatic(stateField.getModifiers()) &&
                                stateField.getName().startsWith("STATE_")) {
                                stateField.setAccessible(true);
                                report("ART_POWER_FETCHER_" + stateField.getName() + "=" +
                                    stateField.get(null));
                            }
                        }
                        if (states[index] != 0) {
                            states[index] = 0;
                            cache[index] = null;
                            report("ART_POWER_CACHE_STATE_RESET=true");
                        }
                    } catch (Throwable failure) {
                        report("ART_POWER_CACHE_SLOT_INSPECTION=" + failure.getClass().getName());
                    }
                }
            }
            for (java.lang.reflect.Method method : current.getDeclaredMethods()) {
                if (method.getName().equals("createService") || method.getName().equals("getService")) {
                    report("ART_POWER_FETCHER_DECLARED_METHOD=" + current.getName() + ":" +
                        method.toGenericString());
                }
                if (method.getName().equals("getService") &&
                    !java.lang.reflect.Modifier.isAbstract(method.getModifiers())) {
                    getServiceMethod = method;
                } else if (method.getName().equals("createService") &&
                    !java.lang.reflect.Modifier.isAbstract(method.getModifiers())) {
                    createServiceMethod = method;
                }
            }
        }
        if (getServiceMethod != null) {
            report("ART_POWER_FETCHER_METHOD=" + getServiceMethod.toGenericString());
            getServiceMethod.setAccessible(true);
            try {
                Object direct = getServiceMethod.invoke(fetcher, context);
                reportService("ART_DIRECT_POWER_FETCHER", direct);
                if (direct == null) {
                    Object retried = getServiceMethod.invoke(fetcher, context);
                    reportService("ART_DIRECT_POWER_FETCHER_RETRY", retried);
                }
            } catch (Throwable failure) {
                report("ART_DIRECT_POWER_FETCHER_FAILURE=" + failure.getClass().getName());
            }
        }
        if (createServiceMethod != null) {
            report("ART_POWER_FACTORY_METHOD=" + createServiceMethod.toGenericString());
            createServiceMethod.setAccessible(true);
            try {
                Object created = createServiceMethod.invoke(fetcher, context);
                reportService("ART_POWER_FACTORY_RESULT", created);
            } catch (Throwable failure) {
                while (failure instanceof java.lang.reflect.InvocationTargetException &&
                    ((java.lang.reflect.InvocationTargetException) failure).getCause() != null) {
                    failure = ((java.lang.reflect.InvocationTargetException) failure).getCause();
                }
                report("ART_POWER_FACTORY_FAILURE=" + failure.getClass().getName());
                report("ART_POWER_FACTORY_FAILURE_MESSAGE=" + failure.getMessage());
            }
        }
    }

    public static void main(String[] args) {
        if (args.length != 1 || !(args[0].equals("system-server") || args[0].equals("display"))) {
            throw new IllegalArgumentException("Expected system-server or display");
        }
        report("ART_PROBE_MAIN_ENTERED=true");
        try {
            if (args[0].equals("system-server")) {
                stage = "SystemServer.main";
                report("ART_SYSTEM_SERVER_ENTERING=true");
                Class.forName("com.android.server.SystemServer").getMethod("main", String[].class)
                    .invoke(null, (Object) new String[0]);
                report("ART_SYSTEM_SERVER_RETURNED=true");
            } else {
                stage = "android_servers.load";
                System.loadLibrary("android_servers");
                report("ART_ANDROID_SERVERS_LOADED=true");
                stage = "system.context";
                Class<?> looper = Class.forName("android.os.Looper");
                looper.getMethod("prepareMainLooper").invoke(null);
                Class<?> threadClass = Class.forName("android.app.ActivityThread");
                Object thread = threadClass.getMethod("systemMain").invoke(null);
                Object context = threadClass.getMethod("getSystemContext").invoke(thread);
                report("ART_SYSTEM_CONTEXT_PRESENT=" + (context != null));
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
                report("ART_POWER_SERVICE_STARTED=" + (power != null));
                stage = "ThermalManagerService.start";
                Object thermal = managerClass.getMethod("startService", String.class)
                    .invoke(manager, "com.android.server.power.ThermalManagerService");
                report("ART_THERMAL_SERVICE_STARTED=" + (thermal != null));
                stage = "PowerManagerService.publication";
                Class<?> serviceManagerClass = Class.forName("android.os.ServiceManager");
                Object thermalBinder = serviceManagerClass.getMethod("checkService", String.class)
                    .invoke(null, "thermalservice");
                reportService("ART_THERMAL_BINDER", thermalBinder);
                Object powerBinder = serviceManagerClass.getMethod("checkService", String.class)
                    .invoke(null, "power");
                reportService("ART_POWER_BINDER", powerBinder);
                Object services = serviceManagerClass.getMethod("listServices").invoke(null);
                boolean powerListed = false;
                if (services instanceof String[]) {
                    for (String name : (String[]) services) {
                        if ("power".equals(name)) {
                            powerListed = true;
                            break;
                        }
                    }
                }
                report("ART_POWER_SERVICE_LISTED=" + powerListed);
                Class<?> contextClass = Class.forName("android.content.Context");
                Class<?> powerManagerClass = Class.forName("android.os.PowerManager");
                Object powerServiceName = contextClass.getMethod("getSystemServiceName", Class.class)
                    .invoke(context, powerManagerClass);
                report("ART_CONTEXT_POWER_SERVICE_NAME=" + powerServiceName);
                stage = "SystemServiceRegistry.fetcher";
                Class<?> registryClass = Class.forName("android.app.SystemServiceRegistry");
                try {
                    java.lang.reflect.Field fetchersField = registryClass.getDeclaredField("SYSTEM_SERVICE_FETCHERS");
                    fetchersField.setAccessible(true);
                    Object fetchers = fetchersField.get(null);
                    Object powerFetcher = ((java.util.Map<?, ?>) fetchers).get("power");
                    reportService("ART_POWER_FETCHER", powerFetcher);
                    reportFetcherDetails(powerFetcher, context);
                } catch (Throwable failure) {
                    report("ART_POWER_FETCHER_INSPECTION=" + failure.getClass().getName());
                }
                Object contextPower = contextClass.getMethod("getSystemService", String.class)
                    .invoke(context, "power");
                reportService("ART_CONTEXT_POWER", contextPower);
                Object contextPowerByClass = contextClass.getMethod("getSystemService", Class.class)
                    .invoke(context, powerManagerClass);
                reportService("ART_CONTEXT_POWER_BY_CLASS", contextPowerByClass);
                try {
                    java.lang.reflect.Field cacheField = context.getClass().getDeclaredField("mServiceCache");
                    cacheField.setAccessible(true);
                    Object[] cache = (Object[]) cacheField.get(context);
                    report("ART_CONTEXT_SERVICE_CACHE_LENGTH=" + cache.length);
                } catch (Throwable failure) {
                    report("ART_CONTEXT_SERVICE_CACHE_INSPECTION=" + failure.getClass().getName());
                }
                Object powerInternal = Class.forName("com.android.server.LocalServices")
                    .getMethod("getService", Class.class)
                    .invoke(null, Class.forName("android.os.PowerManagerInternal"));
                reportService("ART_POWER_INTERNAL", powerInternal);
                stage = "DisplayManagerService.start";
                Object display = managerClass.getMethod("startService", String.class)
                    .invoke(manager, "com.android.server.display.DisplayManagerService");
                System.out.println("ART_DISPLAY_SERVICE_STARTED=" + (display != null));
                stage = "display.binder";
                Object binder = serviceManagerClass.getMethod("checkService", String.class)
                    .invoke(null, "display");
                reportService("ART_DISPLAY_BINDER", binder);
                if (binder != null) {
                    System.out.println("ART_DISPLAY_BINDER_DESCRIPTOR=" + Class.forName("android.os.IBinder")
                        .getMethod("getInterfaceDescriptor").invoke(binder));
                }
            }
            report("ART_SYSTEM_SERVICES_PROBE_RETURNED_LIMITED");
            System.exit(0);
        } catch (Throwable failure) {
            while (failure instanceof java.lang.reflect.InvocationTargetException) {
                failure = ((java.lang.reflect.InvocationTargetException) failure).getCause();
            }
            reportError("ART_SYSTEM_SERVICES_FAILURE_STAGE=" + stage);
            reportError("ART_SYSTEM_SERVICES_FAILURE=" + failure.getClass().getName());
            failure.printStackTrace();
            System.err.flush();
            System.exit(3);
        }
    }
}
