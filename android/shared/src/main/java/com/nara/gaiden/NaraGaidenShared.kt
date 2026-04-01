package com.nara.gaiden

import android.graphics.Color
import kotlin.math.max
import kotlin.math.roundToInt
import org.json.JSONObject
import java.util.Locale

private const val POOP_ALERT_THRESHOLD_MS = 2L * 24L * 60L * 60L * 1000L
private const val POOP_ALERT_EMOJI = "⚠️"

data class NaraGaidenRow(
    val name: String,
    val feedLabel: String,
    val feedBeginDt: Long?,
    val diaperLabel: String,
    val diaperBeginDt: Long?,
    val lastPoopDiaperBeginDt: Long?,
    val vitaminsTodayCount: Int,
    val medicationTodayCount: Int,
    val bathsTodayCount: Int
) {
    val hasPoopAlert: Boolean
        get() = NaraGaidenFormat.formatPoopAlertDays(lastPoopDiaperBeginDt) != null

    val displayName: String
        get() {
            val indicators = buildString {
                if (hasPoopAlert) {
                    append(POOP_ALERT_EMOJI)
                }
                repeat(vitaminsTodayCount.coerceAtLeast(0)) { append("💊") }
                repeat(medicationTodayCount.coerceAtLeast(0)) { append("💉") }
                repeat(bathsTodayCount.coerceAtLeast(0)) { append("🛁") }
            }
            if (indicators.isEmpty()) {
                return name
            }
            return "$name $indicators"
        }

    fun poopAlertText(nowMs: Long = System.currentTimeMillis()): String? {
        val daysText = NaraGaidenFormat.formatPoopAlertDays(lastPoopDiaperBeginDt, nowMs) ?: return null
        return "$POOP_ALERT_EMOJI $name hasn't pooped for $daysText."
    }
}

object NaraGaidenStore {
    const val PREFS_NAME = "nara_gaiden_widget"
    const val KEY_JSON = "last_json"
    const val KEY_UPDATED = "last_updated"
    const val KEY_LAST_SUCCESS_MS = "last_success_ms"
    const val KEY_LAST_ERROR = "last_error"
    const val KEY_GAIDEN_ARMED_MS = "gaiden_armed_ms"
    const val KEY_NARA_ARMED_MS = "nara_armed_ms"
    const val KEY_WEAR_RENDER_ONLY_UNTIL_MS = "wear_render_only_until_ms"
    const val KEY_WEAR_REFRESHING_UNTIL_MS = "wear_refreshing_until_ms"
}

object NaraGaidenContent {
    fun parseRows(rawJson: String): List<NaraGaidenRow> {
        val rows = ArrayList<NaraGaidenRow>()
        val json = JSONObject(rawJson)
        val children = json.optJSONArray("children") ?: return rows
        for (i in 0 until children.length()) {
            val child = children.optJSONObject(i) ?: continue
            val feed = child.optJSONObject("feed")
            val diaper = child.optJSONObject("diaper")
            rows.add(
                NaraGaidenRow(
                    name = child.optString("name", "Unknown"),
                    feedLabel = feed?.optString("label", "unknown") ?: "unknown",
                    feedBeginDt = feed?.optLong("beginDt", 0L)?.takeIf { it > 0 },
                    diaperLabel = diaper?.optString("label", "unknown") ?: "unknown",
                    diaperBeginDt = diaper?.optLong("beginDt", 0L)?.takeIf { it > 0 },
                    lastPoopDiaperBeginDt = child.optLong("lastPoopDiaperBeginDt", 0L).takeIf { it > 0 },
                    vitaminsTodayCount = (
                        if (child.has("vitaminsToday")) {
                            child.optInt("vitaminsToday", 0)
                        } else if (child.has("vitaminsTodayCount")) {
                            child.optInt("vitaminsTodayCount", 0)
                        } else if (child.optBoolean("vitaminsToday", false)) {
                            1
                        } else {
                            0
                        }
                    ).coerceAtLeast(0),
                    medicationTodayCount = (
                        if (child.has("medicationToday")) {
                            child.optInt("medicationToday", 0)
                        } else if (child.has("medicationTodayCount")) {
                            child.optInt("medicationTodayCount", 0)
                        } else if (child.optBoolean("medicationToday", false)) {
                            1
                        } else {
                            0
                        }
                    ).coerceAtLeast(0),
                    bathsTodayCount = (
                        if (child.has("bathsToday")) {
                            child.optInt("bathsToday", 0)
                        } else if (child.has("bathsTodayCount")) {
                            child.optInt("bathsTodayCount", 0)
                        } else if (child.optBoolean("bathsToday", false)) {
                            1
                        } else {
                            0
                        }
                    ).coerceAtLeast(0)
                )
            )
        }
        return rows
    }
}

object NaraGaidenFormat {
    data class TimeColors(val bg: Int, val fg: Int)

    fun compactFeedLabel(label: String): String {
        return label
            .removePrefix("Bottle ")
            .removePrefix("Bottle:")
            .replace("(", "")
            .replace(")", "")
            .replace(Regex("\\s+"), " ")
            .trim()
    }

    fun formatRelative(beginDt: Long?): String {
        if (beginDt == null) {
            return "unknown"
        }
        val nowMs = System.currentTimeMillis()
        val deltaSec = ((nowMs - beginDt) / 1000).coerceAtLeast(0)
        val mins = deltaSec / 60
        val hours = mins / 60
        val days = hours / 24

        val parts = ArrayList<String>()
        if (days > 0) {
            parts.add("$days day" + if (days == 1L) "" else "s")
        }
        val hoursPart = hours % 24
        if (hoursPart > 0) {
            parts.add("$hoursPart hour" + if (hoursPart == 1L) "" else "s")
        }
        val minsPart = mins % 60
        if (minsPart > 0 && days == 0L) {
            val suffix = if (minsPart == 1L) "" else "s"
            parts.add("$minsPart min$suffix")
        }
        if (parts.isEmpty()) {
            return "just now"
        }
        return parts.joinToString(" ") + " ago"
    }

    fun formatRelativeCompact(beginDt: Long?): String {
        if (beginDt == null) {
            return "--"
        }
        val deltaMinutes = ((System.currentTimeMillis() - beginDt) / 60000).coerceAtLeast(0)
        if (deltaMinutes <= 0L) {
            return "now"
        }
        val days = deltaMinutes / (24L * 60L)
        val hours = (deltaMinutes / 60L) % 24L
        val minutes = deltaMinutes % 60L

        if (days > 0L) {
            return if (hours > 0L) {
                "${days}d${hours}h"
            } else {
                "${days}d"
            }
        }
        if (deltaMinutes < 60L) {
            return "${minutes}m"
        }
        return if (minutes > 0L) {
            "${deltaMinutes / 60L}h${minutes}m"
        } else {
            "${deltaMinutes / 60L}h"
        }
    }

    fun timeColors(beginDt: Long?): TimeColors {
        if (beginDt == null) {
            return TimeColors(Color.parseColor("#333333"), Color.parseColor("#f2f2f2"))
        }
        val nowMs = System.currentTimeMillis()
        val deltaHours = max(0.0, (nowMs - beginDt) / 3600000.0)

        val stops = listOf(
            1.0 to intArrayOf(27, 94, 32),
            2.0 to intArrayOf(133, 100, 18),
            3.0 to intArrayOf(121, 69, 0),
            4.0 to intArrayOf(122, 28, 28),
        )

        val rgb = when {
            deltaHours <= 1.0 -> stops[0].second
            deltaHours >= 4.0 -> stops.last().second
            else -> {
                var color = stops.last().second
                for (i in 0 until stops.size - 1) {
                    val (h0, c0) = stops[i]
                    val (h1, c1) = stops[i + 1]
                    if (deltaHours <= h1) {
                        val t = (deltaHours - h0) / (h1 - h0)
                        color = intArrayOf(
                            (c0[0] + (c1[0] - c0[0]) * t).roundToInt(),
                            (c0[1] + (c1[1] - c0[1]) * t).roundToInt(),
                            (c0[2] + (c1[2] - c0[2]) * t).roundToInt(),
                        )
                        break
                    }
                }
                color
            }
        }

        return TimeColors(Color.rgb(rgb[0], rgb[1], rgb[2]), Color.WHITE)
    }

    fun formatPoopAlertDays(lastPoopDiaperBeginDt: Long?, nowMs: Long = System.currentTimeMillis()): String? {
        if (lastPoopDiaperBeginDt == null) {
            return null
        }
        val deltaMs = (nowMs - lastPoopDiaperBeginDt).coerceAtLeast(0L)
        if (deltaMs < POOP_ALERT_THRESHOLD_MS) {
            return null
        }
        val roundedTenths = ((deltaMs.toDouble() / 86_400_000.0) * 10.0).roundToInt().coerceAtLeast(0)
        val value = if (roundedTenths % 10 == 0) {
            (roundedTenths / 10).toString()
        } else {
            String.format(Locale.US, "%.1f", roundedTenths / 10.0)
        }
        val unit = if (roundedTenths == 10) "day" else "days"
        return "$value $unit"
    }

    fun withStaleSuffix(updatedLine: String, lastSuccessMs: Long, include: Boolean): String {
        if (!include || lastSuccessMs <= 0) {
            return updatedLine
        }
        val suffix = formatAgeLabel(lastSuccessMs) ?: return updatedLine
        return "$updatedLine ($suffix)"
    }

    fun formatAgeLabel(
        lastSuccessMs: Long,
        nowMs: Long = System.currentTimeMillis(),
        roundToNearestMinute: Boolean = false
    ): String? {
        if (lastSuccessMs <= 0) {
            return null
        }
        val ageMs = (nowMs - lastSuccessMs).coerceAtLeast(0L)
        val minutes = if (roundToNearestMinute) {
            ((ageMs + 30_000L) / 60_000L).coerceAtLeast(0L)
        } else {
            (ageMs / 60_000L).coerceAtLeast(0L)
        }
        if (minutes == 0L) {
            return null
        }
        return if (minutes == 1L) "1 min old" else "$minutes mins old"
    }
}
