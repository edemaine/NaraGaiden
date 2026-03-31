package com.nara.gaiden.wear

import androidx.wear.protolayout.TimelineBuilders.Timeline
import androidx.wear.protolayout.material3.MaterialScope
import androidx.wear.protolayout.material3.primaryLayout
import androidx.wear.protolayout.material3.text
import androidx.wear.protolayout.types.layoutString
import androidx.wear.tiles.Material3TileService
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders

class NaraWearTileService : Material3TileService() {
    override suspend fun MaterialScope.tileResponse(
        requestParams: RequestBuilders.TileRequest
    ): TileBuilders.Tile {
        NaraWearSyncRequester.requestSnapshot(applicationContext)
        val summary = NaraWearSnapshotPresenter.load(applicationContext)
        return TileBuilders.Tile.Builder()
            .setFreshnessIntervalMillis(FRESHNESS_INTERVAL_MS)
            .setTileTimeline(
                Timeline.fromLayoutElement(
                    primaryLayout(
                        titleSlot = { text(summary.tileTitle.layoutString, maxLines = 1) },
                        mainSlot = { text(summary.tileBody.layoutString, maxLines = 3) },
                        bottomSlot = { text(summary.tileFooter.layoutString, maxLines = 1) }
                    )
                )
            )
            .build()
    }

    companion object {
        private const val FRESHNESS_INTERVAL_MS = 5 * 60 * 1000L
    }
}
