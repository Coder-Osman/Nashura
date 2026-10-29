/** Run the existing guarded native probe in the VM that loaded the real engine. */
public final class ArtNativeInitProbe {
    private static native int run(String engine, String directory,
                                 String contentGuard, String filesystemGuard);

    public static void main(String[] args) throws Exception {
        if (args.length != 6) {
            throw new IllegalArgumentException("Expected engine, observer, driver, output, and two guards");
        }
        ArtEngineLoadProbe.main(new String[] {args[0], args[1]});
        Object context = Class.forName("com.supercell.titan.GameApp")
            .getMethod("getInstance").invoke(null);
        System.out.println("ART_GAME_CONTEXT_PRESENT=" + (context != null));
        System.load(args[2]);
        int status = run(args[0], args[3], args[4], args[5]);
        System.out.println("ART_NATIVE_INIT_EXIT=" + status);
        if (status != 0) {
            throw new AssertionError("Native init probe status=" + status);
        }
        System.out.println("ART_NATIVE_HOLDER_LIFECYCLE_PASS_LIMITED");
    }
}
