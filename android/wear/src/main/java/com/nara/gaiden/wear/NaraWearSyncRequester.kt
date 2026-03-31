package com.nara.gaiden.wear

import android.content.Context
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.Wearable
import com.nara.gaiden.NaraGaidenStore
import com.nara.gaiden.NaraGaidenWearSync
import java.util.concurrent.TimeUnit

object NaraWearSyncRequester {
    fun requestSnapshot(context: Context) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val renderOnlyUntilMs = prefs.getLong(NaraGaidenStore.KEY_WEAR_RENDER_ONLY_UNTIL_MS, 0L)
        if (renderOnlyUntilMs > System.currentTimeMillis()) {
            return
        }
        Thread {
            try {
                val nodes = Tasks.await(
                    Wearable.getNodeClient(context).connectedNodes,
                    NODE_TIMEOUT_SECONDS,
                    TimeUnit.SECONDS
                )
                if (nodes.isEmpty()) {
                    return@Thread
                }
                val messageClient = Wearable.getMessageClient(context)
                nodes.forEach { node ->
                    try {
                        Tasks.await(
                            messageClient.sendMessage(
                                node.id,
                                NaraGaidenWearSync.REQUEST_SNAPSHOT_PATH,
                                ByteArray(0)
                            ),
                            MESSAGE_TIMEOUT_SECONDS,
                            TimeUnit.SECONDS
                        )
                    } catch (_: Exception) {
                    }
                }
            } catch (_: Exception) {
            }
        }.start()
    }

    private const val NODE_TIMEOUT_SECONDS = 10L
    private const val MESSAGE_TIMEOUT_SECONDS = 10L
}
