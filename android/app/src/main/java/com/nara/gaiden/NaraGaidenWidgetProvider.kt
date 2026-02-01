package com.nara.gaiden

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.app.AlarmManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.os.SystemClock
import android.widget.RemoteViews
import androidx.core.content.edit

class NaraGaidenWidgetProvider : AppWidgetProvider() {
    override fun onUpdate(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        scheduleTick(context)
        refreshAll(context, appWidgetManager, appWidgetIds)
    }

    override fun onEnabled(context: Context) {
        super.onEnabled(context)
        scheduleTick(context)
    }

    override fun onDisabled(context: Context) {
        super.onDisabled(context)
        cancelTick(context)
    }

    override fun onReceive(context: Context, intent: Intent) {
        super.onReceive(context, intent)
        val manager = AppWidgetManager.getInstance(context)
        val ids = manager.getAppWidgetIds(ComponentName(context, NaraGaidenWidgetProvider::class.java))
        when (intent.action) {
            ACTION_REFRESH -> refreshAll(context, manager, ids)
            ACTION_TICK -> updateFromCache(context, manager, ids)
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
        appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }

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

    private fun updateFromCache(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lastUpdated = prefs.getString(KEY_UPDATED, null)
        val views = buildRemoteViews(context, NaraGaidenWidgetState.idle(lastUpdated))
        appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, views) }
        appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }
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

    private fun scheduleTick(context: Context) {
        val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        val triggerAt = SystemClock.elapsedRealtime() + TICK_INTERVAL_MS
        alarmManager.setInexactRepeating(
            AlarmManager.ELAPSED_REALTIME,
            triggerAt,
            TICK_INTERVAL_MS,
            tickPendingIntent(context)
        )
    }

    private fun cancelTick(context: Context) {
        val alarmManager = context.getSystemService(Context.ALARM_SERVICE) as AlarmManager
        alarmManager.cancel(tickPendingIntent(context))
    }

    private fun tickPendingIntent(context: Context): PendingIntent {
        val intent = Intent(context, NaraGaidenWidgetProvider::class.java).apply {
            action = ACTION_TICK
        }
        return PendingIntent.getBroadcast(
            context,
            1,
            intent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }

    companion object {
        const val ACTION_REFRESH = "com.nara.gaiden.ACTION_REFRESH"
        const val ACTION_TICK = "com.nara.gaiden.ACTION_TICK"
        private const val PREFS_NAME = "nara_gaiden_widget"
        private const val KEY_JSON = "last_json"
        private const val KEY_UPDATED = "last_updated"
        private const val TICK_INTERVAL_MS = 5 * 60 * 1000L
    }
}
