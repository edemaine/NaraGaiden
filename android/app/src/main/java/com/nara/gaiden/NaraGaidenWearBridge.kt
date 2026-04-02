package com.nara.gaiden

import android.content.Context
import android.util.Log
import androidx.core.content.edit
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.Wearable
import java.util.concurrent.TimeUnit

object NaraGaidenWearBridge {
    fun syncCachedSnapshot(context: Context, nodeId: String? = null) {
        Thread {
            sendSnapshot(context, nodeId)
        }.start()
    }

    fun refreshAndSync(context: Context, nodeId: String? = null) {
        Thread {
            refreshCache(context)
            sendSnapshot(context, nodeId)
        }.start()
    }

    private fun refreshCache(context: Context) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        try {
            Log.d(TAG, "Refreshing phone cache from server")
            val result = NaraGaidenApi.fetch()
            val successMs = System.currentTimeMillis()
            prefs.edit {
                putString(NaraGaidenStore.KEY_JSON, result.json)
                putString(NaraGaidenStore.KEY_UPDATED, result.updatedLine)
                putLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, successMs)
                putBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
            }
            Log.d(TAG, "Phone cache refresh succeeded")
        } catch (_: Exception) {
            prefs.edit {
                putBoolean(NaraGaidenStore.KEY_LAST_ERROR, true)
            }
            Log.w(TAG, "Phone cache refresh failed")
        }
    }

    private fun sendSnapshot(context: Context, nodeId: String?) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val snapshot = NaraGaidenWearSync.snapshotFromPrefs(prefs)
        val nodes = try {
            if (nodeId != null) {
                listOf(nodeId)
            } else {
                Tasks.await(
                    Wearable.getNodeClient(context).connectedNodes,
                    NODE_TIMEOUT_SECONDS,
                    TimeUnit.SECONDS
                ).map { it.id }
            }
        } catch (_: Exception) {
            emptyList()
        }
        if (nodes.isEmpty()) {
            Log.w(TAG, "No nodes available for snapshot sync")
            return
        }
        val payload = NaraGaidenWearSync.toPayload(snapshot)
        val messageClient = Wearable.getMessageClient(context)
        nodes.forEach { targetNodeId ->
            try {
                Log.d(TAG, "Sending snapshot to node=$targetNodeId bytes=${payload.size}")
                Tasks.await(
                    messageClient.sendMessage(
                        targetNodeId,
                        NaraGaidenWearSync.SNAPSHOT_PATH,
                        payload
                    ),
                    MESSAGE_TIMEOUT_SECONDS,
                    TimeUnit.SECONDS
                )
                Log.d(TAG, "Snapshot sent to node=$targetNodeId")
            } catch (_: Exception) {
                Log.w(TAG, "Failed to send snapshot to node=$targetNodeId")
            }
        }
    }

    private const val TAG = "NaraWearPhone"
    private const val NODE_TIMEOUT_SECONDS = 10L
    private const val MESSAGE_TIMEOUT_SECONDS = 10L
}
