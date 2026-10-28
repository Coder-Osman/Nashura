import java.io.ByteArrayOutputStream;
import java.io.DataInputStream;
import java.io.DataOutputStream;
import java.io.EOFException;
import java.io.File;
import java.io.FileInputStream;
import java.io.IOException;
import java.lang.reflect.Constructor;
import java.lang.reflect.InvocationTargetException;
import java.lang.reflect.Method;
import java.nio.file.Files;
import java.security.MessageDigest;
import java.util.Arrays;

public final class BackupJournalProbe {
    private static final String[] VALUES = {
        "nullsroyale.rel.free", "nullsroyale.rel.free", "", "zero\u0000tail",
        "\u4e2d\u6587", "\ud83d\ude00", "\ud800", "\udfff"
    };
    private static Class<?> journalClass;
    private static Constructor<?> constructor;
    private static Method create;
    private static Method append;
    private static java.lang.reflect.Field journalFile;

    private static String hex(byte[] bytes) {
        StringBuilder result = new StringBuilder();
        for (byte value : bytes) result.append(String.format("%02x", value & 255));
        return result.toString();
    }

    private static byte[] read(File file) throws IOException {
        return Files.readAllBytes(file.toPath());
    }

    private static Throwable unwrap(Throwable failure) {
        while (failure instanceof InvocationTargetException) failure = failure.getCause();
        return failure;
    }

    private static void expectedFailure(String name, Method method, Object receiver,
                                        Object argument, String expected) throws Exception {
        Throwable caught = null;
        try {
            method.invoke(receiver, argument);
        } catch (InvocationTargetException failure) {
            caught = unwrap(failure);
        }
        if (caught == null || !caught.getClass().getName().equals(expected)) {
            throw new AssertionError(name + ": " + (caught == null ? "normal return" : caught));
        }
        System.out.println("JOURNAL_EVENT\t{\"case\":\"" + name
                + "\",\"exception\":\"" + expected + "\"}");
    }

    private static void checkRecords(File file, String[] expected) throws Exception {
        try (DataInputStream input = new DataInputStream(new FileInputStream(file))) {
            for (String value : expected) {
                if (!input.readUTF().equals(value)) throw new AssertionError("readUTF mismatch");
            }
            try {
                input.readUTF();
                throw new AssertionError("expected EOF");
            } catch (EOFException expectedEof) {
                if (input.available() != 0) throw new AssertionError("remaining bytes");
            }
        }
    }

    private static void prepare(File root) throws Exception {
        if (!root.isDirectory() || root.list().length != 0) throw new AssertionError("empty owned root required");
        Object journal = create.invoke(null, root);
        File file = (File) journalFile.get(journal);
        if (!file.getCanonicalFile().getParentFile().equals(root.getCanonicalFile())
                || !file.getName().startsWith("journal") || !file.getName().endsWith(".tmp")
                || file.length() != 0) throw new AssertionError("newJournal file contract");
        ByteArrayOutputStream expectedBytes = new ByteArrayOutputStream();
        DataOutputStream reference = new DataOutputStream(expectedBytes);
        for (int index = 0; index < VALUES.length; index++) {
            append.invoke(journal, VALUES[index]);
            reference.writeUTF(VALUES[index]);
            byte[] actual = read(file);
            if (!Arrays.equals(actual, expectedBytes.toByteArray())) throw new AssertionError("append bytes");
            checkRecords(file, Arrays.copyOf(VALUES, index + 1));
            System.out.println("JOURNAL_EVENT\t{\"case\":\"append\",\"index\":" + index
                    + ",\"hex\":\"" + hex(actual) + "\",\"length\":" + actual.length + "}");
        }
        char[] oversized = new char[65536];
        Arrays.fill(oversized, 'a');
        expectedFailure("oversize", append, journal, new String(oversized), "java.io.UTFDataFormatException");
        if (!Arrays.equals(read(file), expectedBytes.toByteArray())) throw new AssertionError("overflow changed file");
        expectedFailure("missing_parent", create, null, new File(root, "absent/child"), "java.io.IOException");
        File regularParent = new File(root, "regular-parent");
        if (!regularParent.createNewFile()) throw new AssertionError("parent fixture");
        expectedFailure("file_as_parent", create, null, regularParent, "java.io.IOException");
        File directoryFile = new File(root, "directory-file");
        if (!directoryFile.mkdir()) throw new AssertionError("directory fixture");
        expectedFailure("directory_as_journal", append, constructor.newInstance(directoryFile),
                "package", "java.io.FileNotFoundException");
        Files.write(new File(root, "journal-name").toPath(), file.getName().getBytes("US-ASCII"));
        System.out.println("JOURNAL_EVENT\t{\"case\":\"prepared\",\"file\":\"" + file.getName()
                + "\",\"records\":" + VALUES.length + "}");
    }

    private static void reopen(File root) throws Exception {
        String name = new String(read(new File(root, "journal-name")), "US-ASCII");
        if (!name.matches("journal[A-Za-z0-9._-]+\\.tmp")) throw new AssertionError("journal name");
        File file = new File(root, name);
        checkRecords(file, VALUES);
        append.invoke(constructor.newInstance(file), "reopened");
        String[] expected = Arrays.copyOf(VALUES, VALUES.length + 1);
        expected[VALUES.length] = "reopened";
        checkRecords(file, expected);
        byte[] bytes = read(file);
        System.out.println("JOURNAL_EVENT\t{\"case\":\"reopened\",\"records\":" + expected.length
                + ",\"hex\":\"" + hex(bytes) + "\",\"sha256\":\""
                + hex(MessageDigest.getInstance("SHA-256").digest(bytes)) + "\"}");
    }

    public static void main(String[] arguments) {
        try {
            if (arguments.length != 2) throw new IllegalArgumentException("mode and owned directory required");
            journalClass = Class.forName("com.android.server.backup.DataChangedJournal");
            constructor = journalClass.getDeclaredConstructor(File.class);
            create = journalClass.getDeclaredMethod("newJournal", File.class);
            append = journalClass.getDeclaredMethod("addPackage", String.class);
            constructor.setAccessible(true);
            create.setAccessible(true);
            append.setAccessible(true);
            journalFile = journalClass.getDeclaredField("mFile");
            journalFile.setAccessible(true);
            System.out.println("JOURNAL_EVENT\t{\"case\":\"identity\",\"class\":\""
                    + journalClass.getName() + "\",\"pid\":" + android.os.Process.myPid()
                    + ",\"tid\":" + android.os.Process.myTid() + "}");
            if (arguments[0].equals("prepare")) prepare(new File(arguments[1]));
            else if (arguments[0].equals("reopen")) reopen(new File(arguments[1]));
            else throw new IllegalArgumentException("unknown mode");
        } catch (Throwable failure) {
            unwrap(failure).printStackTrace(System.err);
            System.exit(1);
        }
    }
}
