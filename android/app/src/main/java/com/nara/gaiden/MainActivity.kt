package com.nara.gaiden

import android.app.AlertDialog
import android.content.Intent
import android.text.InputType
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.edit
import android.graphics.drawable.GradientDrawable
import android.view.View
import java.util.concurrent.atomic.AtomicBoolean

class MainActivity : AppCompatActivity() {
    private lateinit var previewList: LinearLayout
    private lateinit var previewEmpty: TextView
    private lateinit var previewAlerts: TextView
    private lateinit var previewUpdated: TextView
    private lateinit var previewStatus: TextView
    private val refreshHandler = Handler(Looper.getMainLooper())
    private val refreshInFlight = AtomicBoolean(false)
    private var passwordPromptVisible = false
    private var refreshAfterPasswordPrompt = false
    @Volatile private var refreshQueued = false
    private val refreshRunnable = object : Runnable {
        override fun run() {
            refreshData()
            refreshHandler.postDelayed(this, REFRESH_INTERVAL_MS)
        }
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val serverView = findViewById<TextView>(R.id.server_url)
        serverView.text = NaraGaidenConfig.serverUrl

        previewList = findViewById(R.id.app_preview_list)
        previewEmpty = findViewById(R.id.app_preview_empty)
        previewAlerts = findViewById(R.id.app_preview_alerts)
        previewUpdated = findViewById(R.id.app_preview_updated)
        previewStatus = findViewById(R.id.app_preview_status)

        loadFromCache()

        val refreshButton = findViewById<Button>(R.id.refresh_button)
        refreshButton.setOnClickListener {
            refreshData()
        }

        val passwordButton = findViewById<Button>(R.id.password_button)
        passwordButton.setOnClickListener {
            showPasswordPrompt(rejected = false)
        }

        val openButton = findViewById<Button>(R.id.open_nara_button)
        openButton.setOnClickListener {
            NaraGaidenLauncher.launchNaraApp(this)
        }

        val plotsButton = findViewById<Button>(R.id.open_plots_button)
        plotsButton.setOnClickListener {
            NaraGaidenLauncher.launchPlots(this)
        }
    }

    override fun onResume() {
        super.onResume()
        refreshHandler.removeCallbacks(refreshRunnable)
        refreshRunnable.run()
    }

    override fun onPause() {
        super.onPause()
        refreshHandler.removeCallbacks(refreshRunnable)
    }

    private fun loadFromCache() {
        val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
        val rawJson = prefs.getString(NaraGaidenStore.KEY_JSON, null)
        val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
        val updatedLine = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --"
        previewUpdated.text = NaraGaidenFormat.withStaleSuffix(updatedLine, lastSuccessMs, include = true)
        previewStatus.text = if (rawJson != null) "Nara Gaiden" else ""
        if (rawJson == null) {
            renderRows(emptyList())
            return
        }
        try {
            val rows = NaraGaidenContent.parseRows(rawJson)
            renderRows(rows)
        } catch (_: Exception) {
            renderRows(emptyList())
        }
    }

    private fun refreshData() {
        if (!refreshInFlight.compareAndSet(false, true)) {
            refreshQueued = true
            return
        }
        previewStatus.text = "Loading..."
        Thread {
            try {
                val result = NaraGaidenApi.fetch(applicationContext)
                val rows = NaraGaidenContent.parseRows(result.json)
                val successMs = System.currentTimeMillis()
                val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
                prefs.edit {
                    putString(NaraGaidenStore.KEY_JSON, result.json)
                    putString(NaraGaidenStore.KEY_UPDATED, result.updatedLine)
                    putLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, successMs)
                    putBoolean(NaraGaidenStore.KEY_LAST_ERROR, false)
                }
                runOnUiThread {
                    previewUpdated.text = NaraGaidenFormat.withStaleSuffix(result.updatedLine, successMs, include = true)
                    previewStatus.text = "Nara Gaiden"
                    renderRows(rows)
                }
                notifyWidgetFromCache()
                NaraGaidenWearBridge.syncCachedSnapshot(applicationContext)
            } catch (e: NaraGaidenAuthException) {
                if (refreshQueued) {
                    return@Thread
                }
                val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
                val fallbackUpdated = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --"
                val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
                prefs.edit { putBoolean(NaraGaidenStore.KEY_LAST_ERROR, true) }
                runOnUiThread {
                    previewUpdated.text = NaraGaidenFormat.withStaleSuffix(fallbackUpdated, lastSuccessMs, include = true)
                    previewStatus.text = e.message ?: "Password required"
                    showPasswordPrompt(rejected = e.rejected)
                }
            } catch (e: Exception) {
                if (refreshQueued) {
                    return@Thread
                }
                val prefs = getSharedPreferences(NaraGaidenStore.PREFS_NAME, MODE_PRIVATE)
                val fallbackUpdated = prefs.getString(NaraGaidenStore.KEY_UPDATED, null) ?: "as of --"
                val lastSuccessMs = prefs.getLong(NaraGaidenStore.KEY_LAST_SUCCESS_MS, 0L)
                prefs.edit { putBoolean(NaraGaidenStore.KEY_LAST_ERROR, true) }
                runOnUiThread {
                    previewUpdated.text = NaraGaidenFormat.withStaleSuffix(fallbackUpdated, lastSuccessMs, include = true)
                    previewStatus.text = "Error: ${e.message ?: "Fetch failed"}"
                }
            } finally {
                refreshInFlight.set(false)
                if (refreshQueued) {
                    refreshQueued = false
                    runOnUiThread {
                        refreshData()
                    }
                }
            }
        }.start()
    }

    private fun showPasswordPrompt(rejected: Boolean) {
        if (passwordPromptVisible) {
            return
        }
        val input = EditText(this).apply {
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            hint = getString(R.string.password_hint)
            setText(if (rejected) "" else NaraGaidenPasswordStore.get(this@MainActivity).orEmpty())
            setSelection(text?.length ?: 0)
        }
        passwordPromptVisible = true
        val dialog = AlertDialog.Builder(this)
            .setTitle(R.string.password_title)
            .setMessage(
                if (rejected) {
                    getString(R.string.password_rejected_message)
                } else {
                    getString(R.string.password_required_message)
                }
            )
            .setView(input)
            .setNegativeButton(android.R.string.cancel, null)
            .setOnDismissListener {
                passwordPromptVisible = false
                if (refreshAfterPasswordPrompt) {
                    refreshAfterPasswordPrompt = false
                    refreshData()
                }
            }
            .create()
        dialog.setOnShowListener {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).setOnClickListener {
                val password = input.text?.toString().orEmpty()
                NaraGaidenPasswordStore.save(this, password)
                refreshAfterPasswordPrompt = true
                dialog.dismiss()
            }
        }
        dialog.setButton(AlertDialog.BUTTON_POSITIVE, getString(R.string.password_save)) { _, _ -> }
        dialog.show()
    }

    private fun notifyWidgetFromCache() {
        val intent = Intent(this, NaraGaidenWidgetProvider::class.java).apply {
            action = NaraGaidenWidgetProvider.ACTION_TICK
        }
        sendBroadcast(intent)
    }

    private fun renderRows(rows: List<NaraGaidenRow>) {
        previewList.removeAllViews()
        val alertsText = rows.mapNotNull { it.poopAlertText() }.joinToString("\n")
        previewAlerts.text = alertsText
        previewAlerts.visibility = if (alertsText.isEmpty()) View.GONE else View.VISIBLE
        if (rows.isEmpty()) {
            previewEmpty.visibility = View.VISIBLE
            return
        }
        previewEmpty.visibility = View.GONE
        rows.forEachIndexed { index, row ->
            val rowView = layoutInflater.inflate(R.layout.app_row, previewList, false)
            val nameView = rowView.findViewById<TextView>(R.id.app_row_name)
            val feedLabelView = rowView.findViewById<TextView>(R.id.app_row_feed_label)
            val feedWhenView = rowView.findViewById<TextView>(R.id.app_row_feed_when)
            val diaperLabelView = rowView.findViewById<TextView>(R.id.app_row_diaper_label)
            val diaperWhenView = rowView.findViewById<TextView>(R.id.app_row_diaper_when)

            nameView.text = row.displayNameWithWrapOpportunities
            feedLabelView.text = row.feedLabel
            feedWhenView.text = NaraGaidenFormat.formatRelative(row.feedBeginDt)
            diaperLabelView.text = row.diaperLabel
            diaperWhenView.text = NaraGaidenFormat.formatRelative(row.diaperBeginDt)

            applyBadge(feedWhenView, row.feedBeginDt)
            applyBadge(diaperWhenView, row.diaperBeginDt)

            previewList.addView(rowView)
            if (index < rows.lastIndex) {
                previewList.addView(buildRowDivider())
            }
        }
    }

    private fun buildRowDivider(): View {
        return View(this).apply {
            background = getDrawable(R.drawable.widget_divider_horizontal)
            layoutParams = LinearLayout.LayoutParams(
                LinearLayout.LayoutParams.MATCH_PARENT,
                resources.getDimensionPixelSize(R.dimen.widget_divider_thickness)
            ).apply {
                topMargin = resources.getDimensionPixelSize(R.dimen.app_preview_row_divider_margin)
                bottomMargin = resources.getDimensionPixelSize(R.dimen.app_preview_row_divider_margin)
            }
        }
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

    companion object {
        private const val REFRESH_INTERVAL_MS = 60_000L
    }
}
