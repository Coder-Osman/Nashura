package org.nashura.backupfixture;

import android.app.backup.BackupAgentHelper;
import android.app.backup.SharedPreferencesBackupHelper;

public final class ProbeBackupAgent extends BackupAgentHelper {
    @Override
    public void onCreate() {
        addHelper("fixture", new SharedPreferencesBackupHelper(this, "fixture"));
    }
}
