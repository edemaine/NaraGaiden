package com.nara.gaiden

import android.util.Log
import com.google.android.gms.wearable.MessageEvent
import com.google.android.gms.wearable.WearableListenerService

class NaraGaidenWearListenerService : WearableListenerService() {
    override fun onMessageReceived(messageEvent: MessageEvent) {
        Log.d(TAG, "Phone received message path=${messageEvent.path} from=${messageEvent.sourceNodeId}")
        if (messageEvent.path != NaraGaidenWearSync.REQUEST_SNAPSHOT_PATH) {
            super.onMessageReceived(messageEvent)
            return
        }
        NaraGaidenWearBridge.refreshAndSync(applicationContext, messageEvent.sourceNodeId)
    }

    companion object {
        private const val TAG = "NaraWearPhone"
    }
}
