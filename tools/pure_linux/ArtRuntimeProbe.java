/** Runtime-only fixture. Does not load the game or contact Android services. */
public final class ArtRuntimeProbe {
    private static native int exercise();

    public static long callback(long value) {
        return value + 42;
    }

    public static void main(String[] args) {
        if (args.length != 1) {
            throw new IllegalArgumentException("Expected an absolute JNI probe library path");
        }
        long sum = 0;
        for (int i = 0; i < 10000; ++i) {
            sum += i;
        }
        if (sum != 49995000) {
            throw new AssertionError("Java calculation mismatch");
        }
        System.load(args[0]);
        int mask = exercise();
        if (mask != 127) {
            throw new AssertionError("JNI lifecycle check mask=" + mask);
        }
        System.out.println("ART_RUNTIME_PASS_LIMITED sum=" + sum + " jni_mask=" + mask
                + " vm=" + System.getProperty("java.vm.name")
                + " version=" + System.getProperty("java.vm.version"));
    }
}
