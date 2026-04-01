package com.nara.gaiden.wear

import android.content.Intent
import android.util.Log
import androidx.core.content.edit
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.WearableListenerService
import com.nara.gaiden.NaraGaidenStore
import com.nara.gaiden.NaraGaidenWearSync

class NaraWearListenerService : WearableListenerService() {
    override fun onMessageReceived(messageEvent: MessageEvent) {
        Log.d(TAG, "Watch received message path=${messageEvent.path} from=${messageEvent.sourceNodeId} bytes=${messageEvent.data.size}")
        if (messageEvent.path != NaraGaidenWearSync.SNAPSHOT_PATH) {
            super.onMessageReceived(messageEvent)
            return
        }

        val snapshot = NaraGaidenWearSync.fromPayload(messageEvent.data)
        val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
        prefs.edit {
            putString(NaraGaidenStore.KEY_JSON, snapshot.rawJson)
            putString(NaraGaidenStore.KEY_UPDATED, snapshot.updatedLine)
            putLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, snapshot.lastSuccessMs)
            putBoolean(NaraGaidenStore.KEY_LAST_ERROR, snapshot.hasError)
            putLong(NaraGaidenStore.KEY_WEAR_REFRESHING_UNTIL_MS, 0L)
            putLong(
                NaraGaidenStore.KEY_WEAR_RENDER_ONLY_UNTIL_MS,
                System.currentTimeMillis() + RENDER_ONLY_WINDOW_MS
            )
        }
        NaraWearUiUpdater.requestAll(applicationContext)
        sendBroadcast(Intent(ACTION_SNAPSHOT_UPDATED).setPackage(packageName))
    }

    companion object {
        const val ACTION_SNAPSHOT_UPDATED = "com.nara.gaiden.wear.ACTION_SNAPSHOT_UPDATED"
        private const val TAG = "NaraWearWatch"
        private const val RENDER_ONLY_WINDOW_MS = 5_000L
    }
}
