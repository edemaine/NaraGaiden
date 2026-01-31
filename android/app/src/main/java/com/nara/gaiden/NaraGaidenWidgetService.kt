package com.nara.gaiden

import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import android.widget.RemoteViewsService
import org.json.JSONObject

data class NaraGaidenRow(
    val name: String,
    val feedLabel: String,
    val feedWhen: String,
    val diaperLabel: String,
    val diaperWhen: String
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
                        feedWhen = feed?.optString("when", "unknown") ?: "unknown",
                        diaperLabel = diaper?.optString("label", "unknown") ?: "unknown",
                        diaperWhen = diaper?.optString("when", "unknown") ?: "unknown"
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
        views.setTextViewText(R.id.row_feed_when, row.feedWhen)
        views.setTextViewText(R.id.row_diaper_label, row.diaperLabel)
        views.setTextViewText(R.id.row_diaper_when, row.diaperWhen)
        return views
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
