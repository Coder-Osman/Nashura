package org.nashura.backupretry;

import android.app.backup.BackupManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.util.Log;
import java.io.File;
import java.io.FileOutputStream;
import java.io.IOException;
import java.nio.charset.StandardCharsets;

public final class RetryReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!"org.nashura.backupretry.STORE_AND_REQUEST".equals(intent.getAction())) {
            throw new IllegalArgumentException("unexpected action");
        }
        String token = intent.getStringExtra("token");
        if (token == null || !token.matches("[0-9a-f]{32}")) {
            throw new IllegalArgumentException("invalid experiment token");
        }
        SharedPreferences preferences = context.getSharedPreferences("fixture", Context.MODE_PRIVATE);
        File marker = new File(context.getNoBackupFilesDir(), "fail-once");
        if (preferences.contains("token") || marker.exists()) {
            throw new IllegalStateException("one-shot experiment already exists");
        }
        try (FileOutputStream stream = new FileOutputStream(marker)) {
            stream.write(token.getBytes(StandardCharsets.US_ASCII));
            stream.getFD().sync();
        } catch (IOException error) {
            throw new IllegalStateException("failure marker write failed", error);
        }
        if (!preferences.edit().putString("token", token)
                .putString("payload", "retry-v1:" + token).commit()) {
            throw new IllegalStateException("payload commit failed");
        }
        Log.i("NashuraBackupRetry", "STORE_RETURN token=" + token);
        new BackupManager(context).dataChanged();
        Log.i("NashuraBackupRetry", "REQUEST_RETURN token=" + token);
        setResultCode(0);
        setResultData("RETRY_REQUEST_RETURNED:" + token);
    }
}
