package org.nashura.backupretry;

import android.app.backup.BackupAgentHelper;
import android.app.backup.BackupDataOutput;
import android.app.backup.SharedPreferencesBackupHelper;
import android.os.ParcelFileDescriptor;
import android.util.Log;
import java.io.File;
import java.io.IOException;
import java.nio.charset.StandardCharsets;
import java.nio.file.Files;

public final class RetryBackupAgent extends BackupAgentHelper {
    @Override
    public void onCreate() {
        addHelper("fixture", new SharedPreferencesBackupHelper(this, "fixture"));
    }

    @Override
    public void onBackup(ParcelFileDescriptor oldState, BackupDataOutput data,
            ParcelFileDescriptor newState) throws IOException {
        String token = getSharedPreferences("fixture", MODE_PRIVATE).getString("token", "ABSENT");
        if (!token.matches("[0-9a-f]{32}")) {
            throw new IOException("experiment not armed");
        }
        Log.i("NashuraBackupRetry", "AGENT_ENTER token=" + token);
        File marker = new File(getNoBackupFilesDir(), "fail-once");
        if (marker.exists()) {
            if (!token.equals(new String(Files.readAllBytes(marker.toPath()), StandardCharsets.US_ASCII))
                    || !marker.delete()) {
                throw new IOException("failure marker identity or removal failed");
            }
            Log.i("NashuraBackupRetry", "AGENT_FAIL token=" + token);
            throw new IOException("NASHURA_CONTROLLED_AGENT_FAILURE");
        }
        super.onBackup(oldState, data, newState);
        Log.i("NashuraBackupRetry", "AGENT_RETURN token=" + token
                + " new_state_bytes=" + newState.getStatSize());
    }
}
