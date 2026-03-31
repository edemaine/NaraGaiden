package com.nara.gaiden

import android.content.SharedPreferences
import org.json.JSONObject

data class NaraGaidenSnapshot(
    val rawJson: String?,
    val updatedLine: String,
    val lastSuccessMs: Long,
    val hasError: Boolean
)

object NaraGaidenWearSync {
    const val REQUEST_SNAPSHOT_PATH = "/nara/request_snapshot"
    const val SNAPSHOT_PATH = "/nara/snapshot"

    fun snapshotFromPrefs(prefs: SharedPreferences): NaraGaidenSnapshot {
        return NaraGaidenSnapshot(
            rawJson = prefs.getString(NaraGaidenStore.KEY_JSON, null),
            updatedLine = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --",
            lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L),
            hasError = prefs.getBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
        )
    }

    fun toPayload(snapshot: NaraGaidenSnapshot): ByteArray {
        return JSONObject()
            .put("rawJson", snapshot.rawJson)
            .put("updatedLine", snapshot.updatedLine)
            .put("lastSuccessMs", snapshot.lastSuccessMs)
            .put("hasError", snapshot.hasError)
            .toString()
            .toByteArray(Charsets.UTF_8)
    }

    fun fromPayload(payload: ByteArray): NaraGaidenSnapshot {
        val json = JSONObject(payload.toString(Charsets.UTF_8))
        return NaraGaidenSnapshot(
            rawJson = json.optString("rawJson").takeIf { it.isNotEmpty() },
            updatedLine = json.optString("updatedLine", "as of --"),
            lastSuccessMs = json.optLong("lastSuccessMs", 0L),
            hasError = json.optBoolean("hasError", false)
        )
    }
}
