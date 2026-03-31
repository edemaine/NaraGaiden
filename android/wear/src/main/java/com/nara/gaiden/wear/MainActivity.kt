package com.nara.gaiden.wear

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.util.Log
import android.view.View
import android.widget.ImageView
import android.widget.TableLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import com.google.android.gms.tasks.Tasks
import com.google.android.gms.wearable.Wearable
import com.nara.gaiden.NaraGaidenContent
import com.nara.gaiden.NaraGaidenFormat
import com.nara.gaiden.NaraGaidenRow
import com.nara.gaiden.NaraGaidenStore
import com.nara.gaiden.NaraGaidenWearSync
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {
    private lateinit var rowList: TableLayout
    private lateinit var emptyView: TextView
    private lateinit var alertsView: TextView
    private lateinit var updatedView: TextView
    private lateinit var statusView: TextView
    private lateinit var refreshRegion: View
    private lateinit var refreshIcon: ImageView
    private val refreshInFlight = AtomicBoolean(false)
    private val mainHandler = Handler(Looper.getMainLooper())
    private var statusOverride: String? = null
    private val refreshTimeoutRunnable = Runnable {
        if (!refreshInFlight.compareAndSet(true, false)) {
            return@Runnable
        }
        setRefreshEnabled(true)
        statusOverride = getString(R.string.status_refresh_timeout)
        loadFromCache()
        Log.w(TAG, "Watch refresh timed out")
    }

    private val snapshotReceiver = object : BroadcastReceiver() {
        override fun onReceive(context: Context?, intent: Intent?) {
            mainHandler.removeCallbacks(refreshTimeoutRunnable)
            refreshInFlight.set(false)
            setRefreshEnabled(true)
            statusOverride = null
            loadFromCache()
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        rowList = findViewById(R.id.wear_row_list)
        emptyView = findViewById(R.id.wear_empty)
        alertsView = findViewById(R.id.wear_alerts)
        updatedView = findViewById(R.id.wear_updated)
        statusView = findViewById(R.id.wear_status)
        refreshRegion = findViewById(R.id.wear_refresh_region)
        refreshIcon = findViewById(R.id.wear_refresh_icon)

        refreshRegion.setOnClickListener {
            requestSnapshot()
        }

        loadFromCache()
    }

    override fun onStart() {
        super.onStart()
        val filter = IntentFilter(NaraWearListenerService.ACTION_SNAPSHOT_UPDATED)
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            registerReceiver(snapshotReceiver, filter, RECEIVER_NOT_EXPORTED)
        } else {
            registerReceiver(snapshotReceiver, filter)
        }
        requestSnapshot()
    }

    override fun onStop() {
        mainHandler.removeCallbacks(refreshTimeoutRunnable)
        unregisterReceiver(snapshotReceiver)
        super.onStop()
    }

    private fun loadFromCache() {
        val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
        val rawJson = prefs.getString(NaraGaidenStore.KEY_JSON, null)
        val updatedLine = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --"
        val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
        val lastError = prefs.getBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)

        updatedView.text = NaraGaidenFormat.withStaleSuffix(updatedLine, lastSuccessMs, include = lastError)
        val statusText = statusOverride ?: when {
            refreshInFlight.get() -> getString(R.string.status_refreshing)
            lastError -> getString(R.string.status_stale)
            rawJson != null -> getString(R.string.status_ready)
            else -> getString(R.string.status_waiting)
        }
        val showStatus = statusOverride != null || refreshInFlight.get() || lastError || rawJson == null
        statusView.text = statusText
        statusView.visibility = if (showStatus) View.VISIBLE else View.GONE

        if (rawJson == null) {
            renderRows(emptyList())
            return
        }

        try {
            renderRows(NaraGaidenContent.parseRows(rawJson))
        } catch (_: Exception) {
            renderRows(emptyList())
        }
    }

    private fun requestSnapshot() {
        if (!refreshInFlight.compareAndSet(false, true)) {
            return
        }
        statusOverride = getString(R.string.status_refreshing)
        statusView.text = statusOverride
        statusView.visibility = View.VISIBLE
        setRefreshEnabled(false)
        mainHandler.removeCallbacks(refreshTimeoutRunnable)
        mainHandler.postDelayed(refreshTimeoutRunnable, REFRESH_TIMEOUT_MS)
        Log.d(TAG, "Watch requesting snapshot")

        Thread {
            val error = try {
                val nodes = Tasks.await(
                    Wearable.getNodeClient(this).connectedNodes,
                    NODE_TIMEOUT_SECONDS,
                    TimeUnit.SECONDS
                )
                if (nodes.isEmpty()) {
                    getString(R.string.status_no_phone)
                } else {
                    Log.d(TAG, "Watch found ${nodes.size} connected node(s)")
                    val messageClient = Wearable.getMessageClient(this)
                    nodes.forEach { node ->
                        Log.d(TAG, "Watch sending request to node=${node.id}")
                        Tasks.await(
                            messageClient.sendMessage(
                                node.id,
                                NaraGaidenWearSync.REQUEST_SNAPSHOT_PATH,
                                ByteArray(0)
                            ),
                            MESSAGE_TIMEOUT_SECONDS,
                            TimeUnit.SECONDS
                        )
                    }
                    null
                }
            } catch (e: Exception) {
                Log.w(TAG, "Watch request failed", e)
                e.message ?: getString(R.string.status_sync_failed)
            }

            if (error != null) {
                runOnUiThread {
                    mainHandler.removeCallbacks(refreshTimeoutRunnable)
                    refreshInFlight.set(false)
                    setRefreshEnabled(true)
                    statusOverride = error
                    loadFromCache()
                }
            }
        }.start()
    }

    private fun renderRows(rows: List<NaraGaidenRow>) {
        rowList.removeAllViews()
        val alertsText = rows.mapNotNull { it.poopAlertText() }.joinToString("\n")
        alertsView.text = alertsText
        alertsView.visibility = if (alertsText.isEmpty()) View.GONE else View.VISIBLE

        if (rows.isEmpty()) {
            emptyView.visibility = View.VISIBLE
            return
        }

        emptyView.visibility = View.GONE
        rows.forEach { row ->
            val rowView = layoutInflater.inflate(R.layout.wear_table_row, rowList, false)
            val nameView = rowView.findViewById<TextView>(R.id.wear_cell_name)
            val feedLabelView = rowView.findViewById<TextView>(R.id.wear_cell_feed_label)
            val feedWhenView = rowView.findViewById<TextView>(R.id.wear_cell_feed_when)
            val diaperLabelView = rowView.findViewById<TextView>(R.id.wear_cell_diaper_label)
            val diaperWhenView = rowView.findViewById<TextView>(R.id.wear_cell_diaper_when)

            nameView.text = row.displayName
            feedLabelView.text = formatFeedLabel(row.feedLabel)
            feedWhenView.text = NaraGaidenFormat.formatRelativeCompact(row.feedBeginDt)
            diaperLabelView.text = row.diaperLabel
            diaperWhenView.text = NaraGaidenFormat.formatRelativeCompact(row.diaperBeginDt)

            applyBadge(feedWhenView, row.feedBeginDt)
            applyBadge(diaperWhenView, row.diaperBeginDt)
            rowList.addView(rowView)
        }
        rowList.addView(layoutInflater.inflate(R.layout.wear_table_header, rowList, false))
    }

    private fun applyBadge(view: TextView, beginDt: Long?) {
        val colors = NaraGaidenFormat.timeColors(beginDt)
        val radius = resources.displayMetrics.density * 6f
        val drawable = GradientDrawable().apply {
            cornerRadius = radius
            setColor(colors.bg)
        }
        view.background = drawable
        view.setTextColor(colors.fg)
    }

    private fun formatFeedLabel(label: String): String {
        return NaraGaidenFormat.compactFeedLabel(label)
    }

    private fun setRefreshEnabled(enabled: Boolean) {
        refreshRegion.isEnabled = enabled
        refreshRegion.isClickable = enabled
        refreshIcon.alpha = if (enabled) 1f else 0.45f
    }

    companion object {
        private const val TAG = "NaraWearWatch"
        private const val NODE_TIMEOUT_SECONDS = 10L
        private const val MESSAGE_TIMEOUT_SECONDS = 10L
        private const val REFRESH_TIMEOUT_MS = 10_000L
    }
}
