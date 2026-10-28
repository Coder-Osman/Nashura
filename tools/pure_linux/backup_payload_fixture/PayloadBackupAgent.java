package org.nashura.backuppayload;

import android.app.backup.BackupAgentHelper;
import android.app.backup.BackupDataOutput;
import android.app.backup.SharedPreferencesBackupHelper;
import android.os.ParcelFileDescriptor;
import android.os.Process;
import android.util.Log;
import java.io.IOException;

public final class PayloadBackupAgent extends BackupAgentHelper {
    @Override
    public void onCreate() {
        addHelper("fixture", new SharedPreferencesBackupHelper(this, "fixture"));
    }

    @Override
    public void onBackup(ParcelFileDescriptor oldState, BackupDataOutput data,
            ParcelFileDescriptor newState) throws IOException {
        String token = getSharedPreferences("fixture", MODE_PRIVATE).getString("token", "ABSENT");
        Log.i("NashuraBackupPayload", "AGENT_ENTER token=" + token
                + " pid=" + Process.myPid() + " tid=" + Process.myTid());
        super.onBackup(oldState, data, newState);
        Log.i("NashuraBackupPayload", "AGENT_RETURN token=" + token
                + " pid=" + Process.myPid() + " tid=" + Process.myTid()
                + " new_state_bytes=" + newState.getStatSize());
    }
}
