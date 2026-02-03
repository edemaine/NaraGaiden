package com.nara.gaiden

import android.app.PendingIntent
import android.appwidget.AppWidgetManager
import android.appwidget.AppWidgetProvider
import android.app.AlarmManager
import android.content.ComponentName
import android.content.Context
import android.content.Intent
import android.net.Uri
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
            ACTION_OPEN -> handleOpenTap(context, manager, ids)
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
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        prefs.edit { putLong(NaraGaidenStore.KEY_ARMED_MS, 0L) }
        val lastUpdated = prefs.getString(NaraGaidenStore.KEY_UPDATED, null)
        val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
        val baseUpdated = lastUpdated ?: "as of --"
        val loadingUpdated = NaraGaidenFormat.withStaleSuffix(baseUpdated, lastSuccessMs, include = true)
        val loadingViews = buildRemoteViews(context, NaraGaidenWidgetState.loading(loadingUpdated))
        appWidgetIds.forEach { appWidgetManager.updateAppWidget(it, loadingViews) }
        appWidgetIds.forEach { appWidgetManager.notifyAppWidgetViewDataChanged(it, R.id.widget_list) }

        Thread {
            try {
                val state = try {
                    val result = NaraGaidenApi.fetch()
                    val successMs = System.currentTimeMillis()
                    prefs.edit {
                        putString(NaraGaidenStore.KEY_JSON, result.json)
                        putString(NaraGaidenStore.KEY_UPDATED, result.updatedLine)
                        putLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, successMs)
                        putBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
                    }
                    NaraGaidenWidgetState.ready(result.updatedLine)
                } catch (e: Exception) {
                    val fallbackUpdated = prefs.getString(NaraGaidenStore.KEY_UPDATED, null)
                    val storedLastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
                    val updatedLine = NaraGaidenFormat.withStaleSuffix(
                        fallbackUpdated ?: "as of --",
                        storedLastSuccessMs,
                        include = true
                    )
                    prefs.edit {
                        putBoolean(NaraGaidenStore.KEY_LAST_ERROR, true)
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
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val lastUpdated = prefs.getString(NaraGaidenStore.KEY_UPDATED, null)
        val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
        val lastError = prefs.getBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
        val baseUpdated = lastUpdated ?: "as of --"
        val updatedLine = NaraGaidenFormat.withStaleSuffix(baseUpdated, lastSuccessMs, include = lastError)
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

        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val armed = getArmedState(prefs)
        views.setTextViewText(R.id.widget_open, if (armed) "⇗" else "↗")

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

        val openIntent = Intent(context, NaraGaidenWidgetProvider::class.java).apply {
            action = ACTION_OPEN
        }
        val openPendingIntent = PendingIntent.getBroadcast(
            context,
            2,
            openIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        views.setOnClickPendingIntent(R.id.widget_open, openPendingIntent)
        return views
    }

    private fun handleOpenTap(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray
    ) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val armedMs = prefs.getLong(NaraGaidenStore.KEY_ARMED_MS, 0L)
        val isArmed = armedMs > 0 && now - armedMs <= ARM_WINDOW_MS

        if (isArmed) {
            prefs.edit { putLong(NaraGaidenStore.KEY_ARMED_MS, 0L) }
            updateOpenUi(context, appWidgetManager, appWidgetIds, showPrompt = false)
            launchNaraApp(context)
            return
        }

        prefs.edit { putLong(NaraGaidenStore.KEY_ARMED_MS, now) }
        updateOpenUi(context, appWidgetManager, appWidgetIds, showPrompt = true)
    }

    private fun updateOpenUi(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
        showPrompt: Boolean
    ) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val armed = getArmedState(prefs)
        val views = RemoteViews(context.packageName, R.layout.widget_nara)
        views.setTextViewText(R.id.widget_open, if (armed) "⇗" else "↗")
        val statusText = if (showPrompt) PROMPT_TEXT else READY_TEXT
        views.setTextViewText(R.id.widget_status, statusText)
        appWidgetIds.forEach { appWidgetManager.partiallyUpdateAppWidget(it, views) }
    }

    private fun getArmedState(prefs: android.content.SharedPreferences): Boolean {
        val armedMs = prefs.getLong(NaraGaidenStore.KEY_ARMED_MS, 0L)
        if (armedMs <= 0L) {
            return false
        }
        val now = System.currentTimeMillis()
        if (now - armedMs > ARM_WINDOW_MS) {
            prefs.edit { putLong(NaraGaidenStore.KEY_ARMED_MS, 0L) }
            return false
        }
        return true
    }

    private fun launchNaraApp(context: Context) {
        val pm = context.packageManager
        val launch = pm.getLaunchIntentForPackage(NARA_PACKAGE)
        if (launch != null) {
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            try {
                context.startActivity(launch)
            } catch (_: Exception) {
                return
            }
            return
        }

        val storeIntent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("market://details?id=$NARA_PACKAGE")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(storeIntent)
            return
        } catch (_: Exception) {
        }

        val webIntent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("https://play.google.com/store/apps/details?id=$NARA_PACKAGE")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(webIntent)
        } catch (_: Exception) {
            return
        }
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
        const val ACTION_OPEN = "com.nara.gaiden.ACTION_OPEN"
        private const val TICK_INTERVAL_MS = 5 * 60 * 1000L
        private const val ARM_WINDOW_MS = 2_000L
        private const val NARA_PACKAGE = "com.naraorganics.nara"
        private const val READY_TEXT = "Nara Gaiden"
        private const val PROMPT_TEXT = "Tap 2x to launch Nara Baby"
        private val refreshInFlight = AtomicBoolean(false)
    }
}
