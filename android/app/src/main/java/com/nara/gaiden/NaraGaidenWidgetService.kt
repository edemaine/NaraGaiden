package com.nara.gaiden

import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import android.widget.RemoteViewsService

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
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val rawJson = prefs.getString(NaraGaidenStore.KEY_JSON, null) ?: return
        try {
            rows.addAll(NaraGaidenContent.parseRows(rawJson))
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
        val name = if (row.vitaminsToday) "${row.name} 💊" else row.name
        views.setTextViewText(R.id.row_name, name)
        views.setTextViewText(R.id.row_feed_label, row.feedLabel)
        views.setTextViewText(
            R.id.row_feed_when,
            NaraGaidenFormat.formatRelative(row.feedBeginDt)
        )
        views.setTextViewText(R.id.row_diaper_label, row.diaperLabel)
        views.setTextViewText(
            R.id.row_diaper_when,
            NaraGaidenFormat.formatRelative(row.diaperBeginDt)
        )
        applyTimeColors(views, R.id.row_feed_when, row.feedBeginDt)
        applyTimeColors(views, R.id.row_diaper_when, row.diaperBeginDt)
        return views
    }

    private fun applyTimeColors(views: RemoteViews, viewId: Int, beginDt: Long?) {
        val colors = NaraGaidenFormat.timeColors(beginDt)
        views.setInt(viewId, "setBackgroundColor", colors.bg)
        views.setTextColor(viewId, colors.fg)
    }

    override fun getLoadingView(): RemoteViews? = null

    override fun getViewTypeCount(): Int = 1

    override fun getItemId(position: Int): Long = position.toLong()

    override fun hasStableIds(): Boolean = true

}
