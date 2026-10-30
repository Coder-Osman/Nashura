import android.content.Context;
import android.content.ContextWrapper;
import android.content.pm.ApplicationInfo;
import android.content.pm.PackageInfo;
import android.content.pm.PackageManager;
import android.content.res.AssetManager;
import android.content.res.Configuration;
import android.content.res.Resources;
import android.content.res.XmlResourceParser;
import android.util.DisplayMetrics;
import java.io.File;
import org.xmlpull.v1.XmlPullParser;

/** APK-local metadata and files only; no installed-package or Android service model. */
public final class HeadlessApkContext extends ContextWrapper {
    private final String apk;
    private final File root;
    private final AssetManager assets;
    private final LocalPackages packages;
    private final PackageInfo metadata;
    private final Resources resources;

    private final class LocalPackages extends UnsupportedHeadlessPackageManager {
        @Override public PackageInfo getPackageInfo(String name, int flags) throws NameNotFoundException {
            System.out.println("HEADLESS_APK_PACKAGE_INFO=" + name + ",flags=" + flags);
            System.out.flush();
            if (!metadata.packageName.equals(name)) throw new NameNotFoundException(name);
            if (flags != 0) throw unsupported("getPackageInfo flags=" + flags);
            return metadata;
        }
        @Override public ApplicationInfo getApplicationInfo(String name, int flags) throws NameNotFoundException {
            return getPackageInfo(name, flags).applicationInfo;
        }
        @Override public boolean hasSystemFeature(String name) {
            System.out.println("HEADLESS_APK_FEATURE_QUERY=" + name);
            System.out.flush();
            // Observed in the original createGameMain call. This Linux fixture
            // does not provide the Google Play Games HPE environment.
            if (name.equals("com.google.android.play.feature.HPE_EXPERIENCE")) return false;
            return super.hasSystemFeature(name);
        }
    }

    public HeadlessApkContext(String apk, String root, AssetManager assets) throws Exception {
        super(null);
        this.apk = apk;
        this.root = new File(root);
        this.assets = assets;
        DisplayMetrics metrics = new DisplayMetrics();
        metrics.setToDefaults();
        metrics.widthPixels = 720;
        metrics.heightPixels = 1280;
        metrics.densityDpi = 320;
        metrics.density = 2.0f;
        metrics.scaledDensity = 2.0f;
        metrics.xdpi = 320.0f;
        metrics.ydpi = 320.0f;
        Configuration configuration = new Configuration();
        configuration.setToDefaults();
        configuration.setLocale(java.util.Locale.US);
        resources = new Resources(assets, metrics, configuration);
        packages = new LocalPackages();
        metadata = new PackageInfo();
        metadata.applicationInfo = new ApplicationInfo();
        // Read the original binary manifest through the real APK AssetManager.
        // PackageManager.getPackageArchiveInfo also pulls platform parse policy;
        // this adapter promises metadata only, not installed-package semantics.
        String android = "http://schemas.android.com/apk/res/android";
        try (XmlResourceParser xml = assets.openXmlResourceParser("AndroidManifest.xml")) {
            for (int event = xml.getEventType(); event != XmlPullParser.END_DOCUMENT; event = xml.next()) {
                if (event != XmlPullParser.START_TAG) continue;
                if (xml.getName().equals("manifest")) {
                    metadata.packageName = xml.getAttributeValue(null, "package");
                    metadata.versionName = xml.getAttributeValue(android, "versionName");
                    metadata.versionCode = xml.getAttributeIntValue(android, "versionCode", 0);
                    metadata.applicationInfo.packageName = metadata.packageName;
                } else if (xml.getName().equals("uses-sdk")) {
                    metadata.applicationInfo.minSdkVersion = xml.getAttributeIntValue(android, "minSdkVersion", 1);
                    metadata.applicationInfo.targetSdkVersion = xml.getAttributeIntValue(android, "targetSdkVersion", 1);
                }
            }
        }
        if (metadata.packageName == null || metadata.versionName == null || metadata.versionName.startsWith("@")) {
            throw new IllegalArgumentException("Original APK metadata missing");
        }
        System.out.println("HEADLESS_APK_METADATA=" + metadata.packageName + ",version=" + metadata.versionName);
        System.out.flush();
    }

    @Override public PackageManager getPackageManager() { return packages; }
    @Override public String getPackageName() { return metadata.packageName; }
    @Override public AssetManager getAssets() { return assets; }
    @Override public Resources getResources() { return resources; }
    @Override public Context getApplicationContext() { return this; }
    @Override public ApplicationInfo getApplicationInfo() {
        ApplicationInfo value = new ApplicationInfo(metadata.applicationInfo);
        value.sourceDir = apk;
        value.publicSourceDir = apk;
        value.dataDir = new File(root, "data").getPath();
        return value;
    }
    @Override public File getFilesDir() { return new File(root, "data"); }
    @Override public File getCacheDir() { return new File(root, "cache"); }
    @Override public Object getSystemService(String name) {
        System.out.println("HEADLESS_UNIMPLEMENTED_SERVICE=" + name);
        System.out.flush();
        throw new UnsupportedOperationException("Headless service: " + name);
    }
}
