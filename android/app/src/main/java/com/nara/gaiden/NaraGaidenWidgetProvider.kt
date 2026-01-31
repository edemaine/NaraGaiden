package com.nara.gaiden

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.widget.RemoteViews
import androidx.core.content.edit

class NaraGaidenWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        refreshAll(context, appWidgetManager, appWidgetIds)
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        if (intent.action == ACTION_REFRESH) {
            val manager = AppWidgetManager.getInstance(context)
            val ids = manager.getAppWidgetIds(ComponentName(context, NaraGaidenWidgetProvider::class.java))
            refreshAll(context, manager, ids)
        }
    }

    private fun refreshAll(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lastUpdated = prefs.getString(KEY_UPDATED, null)
        val loadingViews = buildRemoteViews(context, NaraGaidenWidgetState.loading(lastUpdated))
        appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, loadingViews) }

        Thread {
            val state = try {
                val result = NaraGaidenApi.fetch()
                prefs.edit {
                    putString(KEY_JSON, result.json)
                    putString(KEY_UPDATED, result.updatedLine)
                }
                NaraGaidenWidgetState.ready(result.updatedLine)
            } catch (e: Exception) {
                val fallbackUpdated = prefs.getString(KEY_UPDATED, null)
                NaraGaidenWidgetState.error(e.message ?: "Fetch failed", fallbackUpdated)
            }
            val views = buildRemoteViews(context, state)
            appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, views) }
            appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }
        }.start()
    }

    private fun buildRemoteViews(context: Context, state: NaraGaidenWidgetState): RemoteViews {
        val views = RemoteViews(context.packageName, R.layout.widget_nara)
        views.setTextViewText(R.id.widget_status, state.statusLine)
        views.setTextViewText(R.id.widget_updated, state.updatedLine)

        val serviceIntent = Intent(context, NaraGaidenWidgetService::class.java)
        views.setRemoteAdapter(R.id.widget_list, serviceIntent)
        views.setEmptyView(R.id.widget_list, R.id.widget_empty)

        val intent = Intent(context, NaraGaidenWidgetProvider::class.java).apply {
            action = ACTION_REFRESH
        }
        val pendingIntent = PendingIntent.getBroadcast(
            context,
            0,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        views.setOnClickPendingIntent(R.id.widget_refresh, pendingIntent)
        return views
    }

    companion object {
        const val ACTION_REFRESH = "com.nara.gaiden.ACTION_REFRESH"
        private const val PREFS_NAME = "nara_gaiden_widget"
        private const val KEY_JSON = "last_json"
        private const val KEY_UPDATED = "last_updated"
    }
}
