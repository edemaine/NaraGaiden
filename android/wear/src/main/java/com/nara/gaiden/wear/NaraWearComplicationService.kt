package com.nara.gaiden.wear

import android.app.PendingIntent
import android.content.Intent
import androidx.wear.watchface.complications.data.ComplicationData
import androidx.wear.watchface.complications.data.ComplicationType
import androidx.wear.watchface.complications.data.LongTextComplicationData
import androidx.wear.watchface.complications.data.NoDataComplicationData
import androidx.wear.watchface.complications.data.PlainComplicationText
import androidx.wear.watchface.complications.data.ShortTextComplicationData
import androidx.wear.watchface.complications.datasource.ComplicationRequest
import androidx.wear.watchface.complications.datasource.SuspendingComplicationDataSourceService

class NaraWearComplicationService : SuspendingComplicationDataSourceService() {
    override suspend fun onComplicationRequest(request: ComplicationRequest): ComplicationData? {
        NaraWearSyncRequester.requestSnapshot(applicationContext)
        val summary = NaraWearSnapshotPresenter.load(applicationContext)
        val tapAction = buildLaunchPendingIntent()
        return when (request.complicationType) {
            ComplicationType.SHORT_TEXT -> ShortTextComplicationData.Builder(
                text = PlainComplicationText.Builder(summary.complicationShortText).build(),
                contentDescription = PlainComplicationText.Builder(summary.complicationDescription).build()
            )
                .setTitle(PlainComplicationText.Builder(summary.complicationShortTitle).build())
                .setTapAction(tapAction)
                .build()

            ComplicationType.LONG_TEXT -> LongTextComplicationData.Builder(
                text = PlainComplicationText.Builder(summary.complicationLongText).build(),
                contentDescription = PlainComplicationText.Builder(summary.complicationDescription).build()
            )
                .setTitle(PlainComplicationText.Builder(summary.tileTitle).build())
                .setTapAction(tapAction)
                .build()

            else -> NoDataComplicationData()
        }
    }

    override fun getPreviewData(type: ComplicationType): ComplicationData {
        val summary = NaraWearSnapshotSummary(
            rows = emptyList(),
            alerts = listOf("⚠️ Sample alert"),
            updatedLine = "as of 9:41 AM",
            ageLine = "5 mins old",
            hasError = false,
            tileTitle = "1 alert",
            tileBody = "⚠️ Sample alert",
            tileFooter = "as of 9:41 AM",
            complicationShortText = "1!",
            complicationShortTitle = "Poop",
            complicationLongText = "⚠️ Sample alert",
            complicationDescription = "Nara sample status"
        )
        return when (type) {
            ComplicationType.SHORT_TEXT -> ShortTextComplicationData.Builder(
                text = PlainComplicationText.Builder(summary.complicationShortText).build(),
                contentDescription = PlainComplicationText.Builder(summary.complicationDescription).build()
            )
                .setTitle(PlainComplicationText.Builder(summary.complicationShortTitle).build())
                .build()

            ComplicationType.LONG_TEXT -> LongTextComplicationData.Builder(
                text = PlainComplicationText.Builder(summary.complicationLongText).build(),
                contentDescription = PlainComplicationText.Builder(summary.complicationDescription).build()
            )
                .setTitle(PlainComplicationText.Builder(summary.tileTitle).build())
                .build()

            else -> NoDataComplicationData()
        }
    }

    private fun buildLaunchPendingIntent(): PendingIntent? {
        val launchIntent = Intent(this, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK or Intent.FLAG_ACTIVITY_CLEAR_TOP)
        }
        return PendingIntent.getActivity(
            this,
            0,
            launchIntent,
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
        )
    }
}
