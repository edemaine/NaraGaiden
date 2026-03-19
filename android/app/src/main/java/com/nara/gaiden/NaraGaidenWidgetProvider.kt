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
            ACTION_OPEN_GAIDEN -> handleOpenTap(context, manager, ids, LaunchTarget.GAIDEN)
            ACTION_OPEN_NARA -> handleOpenTap(context, manager, ids, LaunchTarget.NARA)
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
        clearLaunchArmedState(prefs)
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
        views.setTextViewText(
            R.id.widget_open_gaiden,
            if (getArmedState(prefs, NaraGaidenStore.KEY_GAIDEN_ARMED_MS)) "⇗" else "↗"
        )
        views.setTextViewText(
            R.id.widget_open_nara,
            if (getArmedState(prefs, NaraGaidenStore.KEY_NARA_ARMED_MS)) "N" else "n"
        )

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

        val openGaidenIntent = Intent(context, NaraGaidenWidgetProvider::class.java).apply {
            action = ACTION_OPEN_GAIDEN
        }
        val openGaidenPendingIntent = PendingIntent.getBroadcast(
            context,
            2,
            openGaidenIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        views.setOnClickPendingIntent(R.id.widget_open_gaiden, openGaidenPendingIntent)

        val openNaraIntent = Intent(context, NaraGaidenWidgetProvider::class.java).apply {
            action = ACTION_OPEN_NARA
        }
        val openNaraPendingIntent = PendingIntent.getBroadcast(
            context,
            3,
            openNaraIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
        views.setOnClickPendingIntent(R.id.widget_open_nara, openNaraPendingIntent)
        return views
    }

    private fun handleOpenTap(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
        target: LaunchTarget
    ) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val now = System.currentTimeMillis()
        val armedKey = target.armedKey
        val armedMs = prefs.getLong(armedKey, 0L)
        val isArmed = armedMs > 0 && now - armedMs <= ARM_WINDOW_MS

        if (isArmed) {
            clearLaunchArmedState(prefs)
            updateOpenUi(context, appWidgetManager, appWidgetIds, showPrompt = false)
            when (target) {
                LaunchTarget.GAIDEN -> NaraGaidenLauncher.launchGaidenApp(context)
                LaunchTarget.NARA -> NaraGaidenLauncher.launchNaraApp(context)
            }
            return
        }

        prefs.edit {
            clearLaunchArmedState(this)
            putLong(armedKey, now)
        }
        updateOpenUi(context, appWidgetManager, appWidgetIds, showPrompt = true)
    }

    private fun updateOpenUi(
        context: Context,
        appWidgetManager: AppWidgetManager,
        appWidgetIds: IntArray,
        showPrompt: Boolean
    ) {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        val views = RemoteViews(context.packageName, R.layout.widget_nara)
        val gaidenArmed = getArmedState(prefs, NaraGaidenStore.KEY_GAIDEN_ARMED_MS)
        val naraArmed = getArmedState(prefs, NaraGaidenStore.KEY_NARA_ARMED_MS)
        views.setTextViewText(R.id.widget_open_gaiden, if (gaidenArmed) "⇗" else "↗")
        views.setTextViewText(R.id.widget_open_nara, if (naraArmed) "N" else "n")
        val statusText = if (showPrompt) {
            when {
                gaidenArmed -> PROMPT_TEXT_GAIDEN
                naraArmed -> PROMPT_TEXT_NARA
                else -> READY_TEXT
            }
        } else {
            READY_TEXT
        }
        views.setTextViewText(R.id.widget_status, statusText)
        appWidgetIds.forEach { appWidgetManager.partiallyUpdateAppWidget(it, views) }
    }

    private fun getArmedState(
        prefs: android.content.SharedPreferences,
        key: String
    ): Boolean {
        val armedMs = prefs.getLong(key, 0L)
        if (armedMs <= 0L) {
            return false
        }
        val now = System.currentTimeMillis()
        if (now - armedMs > ARM_WINDOW_MS) {
            prefs.edit { putLong(key, 0L) }
            return false
        }
        return true
    }

    private fun clearLaunchArmedState(prefs: android.content.SharedPreferences) {
        prefs.edit {
            clearLaunchArmedState(this)
        }
    }

    private fun clearLaunchArmedState(editor: android.content.SharedPreferences.Editor) {
        editor.putLong(NaraGaidenStore.KEY_GAIDEN_ARMED_MS, 0L)
        editor.putLong(NaraGaidenStore.KEY_NARA_ARMED_MS, 0L)
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
        const val ACTION_OPEN_GAIDEN = "com.nara.gaiden.ACTION_OPEN_GAIDEN"
        const val ACTION_OPEN_NARA = "com.nara.gaiden.ACTION_OPEN_NARA"
        private const val TICK_INTERVAL_MS = 5 * 60 * 1000L
        private const val ARM_WINDOW_MS = 2_000L
        private const val READY_TEXT = "Nara Gaiden"
        private const val PROMPT_TEXT_GAIDEN = "Tap 2x to launch Nara Gaiden"
        private const val PROMPT_TEXT_NARA = "Tap 2x to launch Nara Baby"
        private val refreshInFlight = AtomicBoolean(false)
    }

    private enum class LaunchTarget(val armedKey: String) {
        GAIDEN(NaraGaidenStore.KEY_GAIDEN_ARMED_MS),
        NARA(NaraGaidenStore.KEY_NARA_ARMED_MS)
    }
}
