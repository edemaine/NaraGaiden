package com.nara.gaiden.wear

import android.content.ComponentName
import android.content.Context
import androidx.wear.tiles.TileService
import androidx.wear.watchface.complications.datasource.ComplicationDataSourceUpdateRequester

object NaraWearUiUpdater {
    fun requestAll(context: Context) {
        TileService.getUpdater(context).requestUpdate(NaraWearTileService::class.java)
        ComplicationDataSourceUpdateRequester
            .create(context, ComponentName(context, NaraWearComplicationService::class.java))
            .requestUpdateAll()
    }
}
