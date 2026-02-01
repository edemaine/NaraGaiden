package com.nara.gaiden

import android.content.Context
import android.content.Intent
import android.graphics.Color
import android.widget.RemoteViews
import android.widget.RemoteViewsService
import kotlin.math.max
import kotlin.math.roundToInt
import org.json.JSONObject

data class NaraGaidenRow(
    val name: String,
    val feedLabel: String,
    val feedBeginDt: Long?,
    val diaperLabel: String,
    val diaperBeginDt: Long?
)

class NaraGaidenWidgetService : RemoteViewsService() {
    override fun onGetViewFactory(intent: Intent): RemoteViewsFactory {
        return NaraGaidenWidgetFactory(applicationContext)
    }
}

class NaraGaidenWidgetFactory(private val context: Context) : RemoteViewsService.RemoteViewsFactory {
    private val rows = ArrayList<NaraGaidenRow>()

    override fun onCreate() {
        rows.clear()
    }

    override fun onDataSetChanged() {
        rows.clear()
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val rawJson = prefs.getString(KEY_JSON, null) ?: return
        try {
            val json = JSONObject(rawJson)
            val children = json.optJSONArray("children") ?: return
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
                        diaperBeginDt = diaper?.optLong("beginDt", 0L)?.takeIf { it > 0 }
                    )
                )
            }
        } catch (_: Exception) {
            rows.clear()
        }
    }

    override fun onDestroy() {
        rows.clear()
    }

    override fun getCount(): Int = rows.size

    override fun getViewAt(position: Int): RemoteViews {
        val row = rows[position]
        val views = RemoteViews(context.packageName, R.layout.widget_row)
        views.setTextViewText(R.id.row_name, row.name)
        views.setTextViewText(R.id.row_feed_label, row.feedLabel)
        views.setTextViewText(
            R.id.row_feed_when,
            formatRelative(row.feedBeginDt)
        )
        views.setTextViewText(R.id.row_diaper_label, row.diaperLabel)
        views.setTextViewText(
            R.id.row_diaper_when,
            formatRelative(row.diaperBeginDt)
        )
        applyTimeColors(views, R.id.row_feed_when, row.feedBeginDt)
        applyTimeColors(views, R.id.row_diaper_when, row.diaperBeginDt)
        return views
    }

    private fun formatRelative(beginDt: Long?): String {
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

    private fun applyTimeColors(views: RemoteViews, viewId: Int, beginDt: Long?) {
        val colors = timeColors(beginDt)
        views.setInt(viewId, "setBackgroundColor", colors.bg)
        views.setTextColor(viewId, colors.fg)
    }

    private data class TimeColors(val bg: Int, val fg: Int)

    private fun timeColors(beginDt: Long?): TimeColors {
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

    override fun getLoadingView(): RemoteViews? = null

    override fun getViewTypeCount(): Int = 1

    override fun getItemId(position: Int): Long = position.toLong()

    override fun hasStableIds(): Boolean = true

    companion object {
        private const val PREFS_NAME = "nara_gaiden_widget"
        private const val KEY_JSON = "last_json"
    }
}
