package org.nashura.backuppayload;

import android.app.backup.BackupManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;
import android.os.Process;
import android.util.Log;

public final class PayloadReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!"org.nashura.backuppayload.STORE_AND_REQUEST".equals(intent.getAction())) {
            throw new IllegalArgumentException("unexpected action");
        }
        String token = intent.getStringExtra("token");
        if (token == null || !token.matches("[0-9a-f]{32}")) {
            throw new IllegalArgumentException("invalid experiment token");
        }
        SharedPreferences preferences = context.getSharedPreferences("fixture", Context.MODE_PRIVATE);
        if (preferences.contains("token")) {
            throw new IllegalStateException("one-shot payload already exists");
        }
        if (!preferences.edit().putString("token", token)
                .putString("payload", "payload-v1:" + token).commit()) {
            throw new IllegalStateException("payload commit failed");
        }
        Log.i("NashuraBackupPayload", "STORE_RETURN token=" + token
                + " pid=" + Process.myPid() + " tid=" + Process.myTid());
        new BackupManager(context).dataChanged();
        Log.i("NashuraBackupPayload", "REQUEST_RETURN token=" + token
                + " pid=" + Process.myPid() + " tid=" + Process.myTid());
        setResultCode(0);
        setResultData("PAYLOAD_REQUEST_RETURNED:" + token);
    }
}
