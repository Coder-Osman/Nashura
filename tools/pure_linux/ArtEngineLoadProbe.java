/** Observe System.load and the game's own JNI_OnLoad without invoking battle code. */
public final class ArtEngineLoadProbe {
    private static native int inspect();

    public static void main(String[] args) throws Exception {
        if (args.length != 2 || !args[0].startsWith("/") || !args[1].startsWith("/")) {
            throw new IllegalArgumentException("Expected absolute engine and observer library paths");
        }
        java.security.MessageDigest digest = java.security.MessageDigest.getInstance("SHA-256");
        try (java.io.FileInputStream source = new java.io.FileInputStream(args[0])) {
            byte[] block = new byte[65536];
            int count;
            while ((count = source.read(block)) != -1) {
                digest.update(block, 0, count);
            }
        }
        StringBuilder identity = new StringBuilder();
        for (byte value : digest.digest()) {
            identity.append(String.format("%02x", value & 255));
        }
        if (!identity.toString().equals(
                "110aa2b5cac391c498645e072b0d88729428c2c2845e7e2737ca8ee979059783")) {
            throw new IllegalArgumentException("Engine identity mismatch");
        }
        System.out.println("ART_ENGINE_SYSTEM_LOAD_ENTER");
        System.out.flush();
        System.load(args[0]);
        System.load(args[1]);
        int mask = inspect();
        if (mask != 7) {
            throw new AssertionError("Engine VM observer mask=" + mask);
        }
        System.out.println("ART_ENGINE_VM_BINDING_PASS_LIMITED mask=" + mask);
    }
}
