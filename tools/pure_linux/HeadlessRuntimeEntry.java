import java.util.Arrays;

/** AndroidRuntime.start supplies the entry class as the first array element. */
public final class HeadlessRuntimeEntry {
    public static void main(String[] args) throws Exception {
        if (args.length < 1 || !HeadlessRuntimeEntry.class.getName().equals(args[0])) {
            throw new IllegalArgumentException("Expected AndroidRuntime entry-class prefix");
        }
        System.out.println("HEADLESS_DIRECT_RUNTIME_ENTRY=1");
        System.out.flush();
        ArtHeadlessCreateProbe.main(Arrays.copyOfRange(args, 1, args.length));
        // RuntimeInit normally terminates app_process after main. This direct
        // entry exits only after the probe returns normally, including cleanup.
        System.out.println("HEADLESS_DIRECT_RUNTIME_PROBE_RETURN=1");
        System.out.flush();
        System.exit(0);
    }
}
