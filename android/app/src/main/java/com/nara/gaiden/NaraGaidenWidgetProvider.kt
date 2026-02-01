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
import java.util.concurrent.atomic.AtomicBoolean

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
        if (!refreshInFlight.compareAndSet(false, true)) {
            return
        }
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lastUpdated = prefs.getString(KEY_UPDATED, null)
        val lastSuccessMs = prefs.getLong(KEY_LAST_SUCCESS_MS, 0L)
        val baseUpdated = lastUpdated ?: "as of --"
        val loadingUpdated = withStaleSuffix(baseUpdated, lastSuccessMs, include = true)
        val loadingViews = buildRemoteViews(context, NaraGaidenWidgetState.loading(loadingUpdated))
        appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, loadingViews) }
        appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }

        Thread {
            try {
                val state = try {
                    val result = NaraGaidenApi.fetch()
                    val successMs = System.currentTimeMillis()
                    prefs.edit {
                        putString(KEY_JSON, result.json)
                        putString(KEY_UPDATED, result.updatedLine)
                        putLong(KEY_LAST_SUCCESS_MS, successMs)
                        putBoolean(KEY_LAST_ERROR, false)
                    }
                    NaraGaidenWidgetState.ready(result.updatedLine)
                } catch (e: Exception) {
                    val fallbackUpdated = prefs.getString(KEY_UPDATED, null)
                    val storedLastSuccessMs = prefs.getLong(KEY_LAST_SUCCESS_MS, 0L)
                    val updatedLine = withStaleSuffix(
                        fallbackUpdated ?: "as of --",
                        storedLastSuccessMs,
                        include = true
                    )
                    prefs.edit {
                        putBoolean(KEY_LAST_ERROR, true)
                    }
                    NaraGaidenWidgetState.error(e.message ?: "Fetch failed", updatedLine)
                }
                val views = buildRemoteViews(context, state)
                appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, views) }
                appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }
            } finally {
                refreshInFlight.set(false)
            }
        }.start()
    }

    private fun updateFromCache(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        val prefs = context.getSharedPreferences(PREFS_NAME, Context.MODE_PRIVATE)
        val lastUpdated = prefs.getString(KEY_UPDATED, null)
        val lastSuccessMs = prefs.getLong(KEY_LAST_SUCCESS_MS, 0L)
        val lastError = prefs.getBoolean(KEY_LAST_ERROR, false)
        val baseUpdated = lastUpdated ?: "as of --"
        val updatedLine = withStaleSuffix(baseUpdated, lastSuccessMs, include = lastError)
        val views = buildRemoteViews(context, NaraGaidenWidgetState.idle(updatedLine))
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

    private fun withStaleSuffix(updatedLine: String, lastSuccessMs: Long, include: Boolean): String {
        if (!include || lastSuccessMs <= 0) {
            return updatedLine
        }
        val minutes = ((System.currentTimeMillis() - lastSuccessMs) / 60000).coerceAtLeast(0)
        if (minutes == 0L) {
            return updatedLine
        }
        val suffix = if (minutes == 1L) "1 min old" else "$minutes mins old"
        return "$updatedLine ($suffix)"
    }

    companion object {
        const val ACTION_REFRESH = "com.nara.gaiden.ACTION_REFRESH"
        const val ACTION_TICK = "com.nara.gaiden.ACTION_TICK"
        private const val PREFS_NAME = "nara_gaiden_widget"
        private const val KEY_JSON = "last_json"
        private const val KEY_UPDATED = "last_updated"
        private const val KEY_LAST_SUCCESS_MS = "last_success_ms"
        private const val KEY_LAST_ERROR = "last_error"
        private const val TICK_INTERVAL_MS = 5 * 60 * 1000L
        private val refreshInFlight = AtomicBoolean(false)
    }
}
