package com.nara.gaiden.wear

import android.content.Context
import com.nara.gaiden.NaraGaidenContent
import com.nara.gaiden.NaraGaidenFormat
import com.nara.gaiden.NaraGaidenRow
import com.nara.gaiden.NaraGaidenStore

data class NaraWearSnapshotSummary(
    val rows: List<NaraGaidenRow>,
    val alerts: List<String>,
    val updatedLine: String,
    val hasError: Boolean,
    val tileTitle: String,
    val tileBody: String,
    val tileFooter: String,
    val complicationShortText: String,
    val complicationShortTitle: String,
    val complicationLongText: String,
    val complicationDescription: String
)

object NaraWearSnapshotPresenter {
    fun load(context: Context): NaraWearSnapshotSummary {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val rawJson = prefs.getString(NaraGaidenStore.KEY_JSON, null)
        val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
        val hasError = prefs.getBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
        val updatedLine = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --"
        val footer = NaraGaidenFormat.withStaleSuffix(updatedLine, lastSuccessMs, include = hasError)
        val rows = parseRows(rawJson)
        val alerts = rows.mapNotNull { it.poopAlertText() }
        val firstRow = rows.firstOrNull()

        val tileTitle = when {
            alerts.isNotEmpty() -> if (alerts.size == 1) "1 alert" else "${alerts.size} alerts"
            firstRow != null -> firstRow.name
            else -> "Nara Wear"
        }
        val tileBody = when {
            alerts.isNotEmpty() -> alerts.first()
            firstRow != null -> {
                val feed = NaraGaidenFormat.formatRelativeCompact(firstRow.feedBeginDt)
                val diaper = NaraGaidenFormat.formatRelativeCompact(firstRow.diaperBeginDt)
                "Feed $feed, diaper $diaper"
            }
            else -> "No synced data yet"
        }
        val shortText = when {
            alerts.isNotEmpty() -> "${alerts.size}!"
            firstRow != null -> NaraGaidenFormat.formatRelativeCompact(firstRow.feedBeginDt)
            else -> "--"
        }
        val shortTitle = when {
            alerts.isNotEmpty() -> "Poop"
            firstRow != null -> firstRow.name
            else -> "Nara"
        }
        val longText = when {
            alerts.isNotEmpty() -> alerts.first()
            firstRow != null -> {
                val feed = NaraGaidenFormat.formatRelativeCompact(firstRow.feedBeginDt)
                "${firstRow.name} feed $feed"
            }
            else -> "No synced data"
        }

        return NaraWearSnapshotSummary(
            rows = rows,
            alerts = alerts,
            updatedLine = updatedLine,
            hasError = hasError,
            tileTitle = tileTitle,
            tileBody = tileBody,
            tileFooter = footer,
            complicationShortText = shortText,
            complicationShortTitle = shortTitle,
            complicationLongText = longText,
            complicationDescription = "$tileTitle. $tileBody. $footer"
        )
    }

    private fun parseRows(rawJson: String?): List<NaraGaidenRow> {
        if (rawJson.isNullOrBlank()) {
            return emptyList()
        }
        return try {
            NaraGaidenContent.parseRows(rawJson)
        } catch (_: Exception) {
            emptyList()
        }
    }
}
