package org.nashura.backupfixture;

import android.app.backup.BackupManager;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.os.Process;
import android.util.Log;

public final class ProbeReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        if (!"org.nashura.backupfixture.REQUEST".equals(intent.getAction())) {
            throw new IllegalArgumentException("unexpected action");
        }
        new BackupManager(context).dataChanged();
        Log.i("NashuraBackupFixture", "dataChanged returned uid=" + Process.myUid());
        setResultCode(0);
        setResultData("REQUEST_RETURNED");
    }
}
