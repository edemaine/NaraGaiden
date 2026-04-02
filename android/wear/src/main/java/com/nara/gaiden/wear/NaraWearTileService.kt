package com.nara.gaiden.wear

import android.content.ComponentName
import android.content.Context
import androidx.wear.protolayout.ColorBuilders.argb
import androidx.wear.protolayout.ActionBuilders.launchAction
import androidx.wear.protolayout.DimensionBuilders.dp
import androidx.wear.protolayout.DimensionBuilders.expand
import androidx.wear.protolayout.DimensionBuilders.wrap
import androidx.wear.protolayout.LayoutElementBuilders
import androidx.wear.protolayout.ModifiersBuilders
import androidx.wear.protolayout.TimelineBuilders.Timeline
import androidx.wear.protolayout.layout.androidImageResource
import androidx.wear.protolayout.layout.basicImage
import androidx.wear.protolayout.layout.imageResource
import androidx.wear.protolayout.material3.MaterialScope
import androidx.wear.protolayout.material3.Typography
import androidx.wear.protolayout.material3.text
import androidx.wear.protolayout.modifiers.clickable
import androidx.wear.protolayout.modifiers.loadAction
import androidx.wear.protolayout.types.layoutString
import androidx.wear.tiles.Material3TileService
import androidx.wear.tiles.RequestBuilders
import androidx.wear.tiles.TileBuilders
import com.nara.gaiden.NaraGaidenFormat
import com.nara.gaiden.NaraGaidenRow
import com.nara.gaiden.NaraGaidenStore

class NaraWearTileService : Material3TileService() {
    override suspend fun MaterialScope.tileResponse(
        requestParams: RequestBuilders.TileRequest
    ): TileBuilders.Tile {
        val lastClickableId = requestParams.currentState.lastClickableId
        val didTapRefresh = lastClickableId == REFRESH_CLICK_ID || lastClickableId == BOTTOM_REFRESH_CLICK_ID
        val shouldAutoRefresh = lastClickableId.isEmpty()
        val summary = NaraWearSnapshotPresenter.load(applicationContext)
        val didStartRefresh = when {
            didTapRefresh -> NaraWearSyncRequester.requestSnapshot(applicationContext)
            shouldAutoRefresh -> NaraWearSyncRequester.requestSnapshot(applicationContext)
            else -> false
        }
        val isRefreshing = isTileRefreshInFlight(applicationContext) || didStartRefresh
        return TileBuilders.Tile.Builder()
            .setFreshnessIntervalMillis(FRESHNESS_INTERVAL_MS)
            .setTileTimeline(
                Timeline.fromLayoutElement(
                    buildTileLayout(summary, summary.rows, isRefreshing)
                )
            )
            .build()
    }

    private fun isTileRefreshInFlight(context: Context): Boolean {
        val prefs = context.getSharedPreferences(NaraGaidenStore.PREFS_NAME, Context.MODE_PRIVATE)
        return prefs.getLong(NaraGaidenStore.KEY_WEAR_REFRESHING_UNTIL_MS, 0L) > System.currentTimeMillis()
    }

    private fun MaterialScope.buildTileLayout(
        summary: NaraWearSnapshotSummary,
        rows: List<NaraGaidenRow>,
        isRefreshing: Boolean
    ): LayoutElementBuilders.LayoutElement {
        val column = LayoutElementBuilders.Column.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)

        column.addContent(buildTopStatus(summary.updatedLine, summary.ageLine))

        if (rows.isEmpty()) {
            column.addContent(buildVerticalSpacer(4f))
            column.addContent(
                text(summary.tileBody.layoutString, maxLines = 3)
            )
            column.addContent(buildBottomRefreshArea(isRefreshing))
        } else {
            column.addContent(buildFillSpacer())
            column.addContent(buildVerticalSpacer(2f))
            column.addContent(buildTable(rows))
            column.addContent(buildHeaderRow())
            column.addContent(buildBottomRefreshArea(isRefreshing))
        }

        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setPadding(
                        ModifiersBuilders.Padding.Builder()
                            .setStart(dp(0f))
                            .setEnd(dp(0f))
                            .setTop(dp(2f))
                            .setBottom(dp(7f))
                            .build()
                    )
                    .build()
            )
            .addContent(column.build())
            .build()
    }

    private fun MaterialScope.buildTopStatus(updatedLine: String, ageLine: String?): LayoutElementBuilders.LayoutElement {
        val column = LayoutElementBuilders.Column.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setClickable(clickable(id = REFRESH_CLICK_ID, action = loadAction()))
                    .build()
            )
        column.addContent(
            text(
                updatedLine.layoutString,
                maxLines = 1,
                typography = Typography.BODY_SMALL,
                color = colorScheme.onSurfaceVariant
            )
        )
        if (ageLine != null) {
            column.addContent(
                text(
                    ageLine.layoutString,
                    maxLines = 1,
                    typography = Typography.BODY_SMALL,
                    color = colorScheme.onSurfaceVariant
                )
            )
        } else {
            column.addContent(
                text(
                    " ".layoutString,
                    maxLines = 1,
                    typography = Typography.BODY_SMALL,
                    color = colorScheme.onSurfaceVariant
                )
            )
        }
        return column.build()
    }

    private fun MaterialScope.buildBottomStatus(status: String): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .addContent(
                text(
                    status.layoutString,
                    maxLines = 1,
                    typography = Typography.BODY_SMALL,
                    color = colorScheme.onSurfaceVariant
                )
            )
            .build()
    }

    private fun MaterialScope.buildBottomRefreshArea(isRefreshing: Boolean): LayoutElementBuilders.LayoutElement {
        val column = LayoutElementBuilders.Column.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)

        if (isRefreshing) {
            column.addContent(buildBottomStatus("Refreshing..."))
        } else {
            column.addContent(buildVerticalSpacer(12f))
        }
        column.addContent(
            buildBottomIcon()
        )
        column.addContent(
            buildVerticalSpacer(10f)
        )

        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_CENTER)
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setClickable(clickable(id = BOTTOM_REFRESH_CLICK_ID, action = loadAction()))
                    .build()
            )
            .addContent(column.build())
            .build()
    }

    private fun MaterialScope.buildBottomIcon(): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .addContent(
                protoLayoutScope.basicImage(
                    resource = imageResource(androidImage = androidImageResource(R.drawable.ic_sync)),
                    width = dp(18f),
                    height = dp(18f)
                )
            )
            .build()
    }

    private fun buildVerticalSpacer(heightDp: Float): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Spacer.Builder()
            .setWidth(expand())
            .setHeight(dp(heightDp))
            .build()
    }

    private fun buildFillSpacer(): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Spacer.Builder()
            .setWidth(expand())
            .setHeight(expand())
            .build()
    }

    private fun MaterialScope.buildTable(rows: List<NaraGaidenRow>): LayoutElementBuilders.LayoutElement {
        val activityComponent = ComponentName(applicationContext, MainActivity::class.java)
        val column = LayoutElementBuilders.Column.Builder()
            .setWidth(wrap())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_START)

        rows.forEachIndexed { index, row ->
            if (index > 0) {
                column.addContent(buildRowSeparator())
            }
            column.addContent(buildDataRow(row))
        }

        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setClickable(clickable(action = launchAction(activityComponent)))
                    .build()
            )
            .addContent(column.build())
            .build()
    }

    private fun MaterialScope.buildDataRow(row: NaraGaidenRow): LayoutElementBuilders.LayoutElement {
        val rowBuilder = LayoutElementBuilders.Row.Builder()
            .setWidth(wrap())
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_TOP)

        rowBuilder.addContent(
            buildCell(
                widthDp = 52f,
                primary = row.displayName,
                secondary = null,
                secondaryPillDt = null,
                alignCenter = true,
                emphasize = true
            )
        )
        rowBuilder.addContent(buildHorizontalSpacer(2f))
        rowBuilder.addContent(
            buildCell(
                widthDp = 56f,
                primary = NaraGaidenFormat.compactFeedLabel(row.feedLabel),
                secondary = NaraGaidenFormat.formatRelativeCompact(row.feedBeginDt),
                secondaryPillDt = row.feedBeginDt,
                alignCenter = true,
                emphasize = false
            )
        )
        rowBuilder.addContent(buildHorizontalSpacer(2f))
        rowBuilder.addContent(
            buildCell(
                widthDp = 56f,
                primary = row.diaperLabel,
                secondary = NaraGaidenFormat.formatRelativeCompact(row.diaperBeginDt),
                secondaryPillDt = row.diaperBeginDt,
                alignCenter = true,
                emphasize = false
            )
        )
        return rowBuilder.build()
    }

    private fun MaterialScope.buildHeaderRow(): LayoutElementBuilders.LayoutElement {
        val rowBuilder = LayoutElementBuilders.Row.Builder()
            .setWidth(wrap())
            .setVerticalAlignment(LayoutElementBuilders.VERTICAL_ALIGN_TOP)

        rowBuilder.addContent(buildHeaderCell(52f, "CHILD"))
        rowBuilder.addContent(buildHorizontalSpacer(2f))
        rowBuilder.addContent(buildHeaderCell(56f, "FEED"))
        rowBuilder.addContent(buildHorizontalSpacer(2f))
        rowBuilder.addContent(buildHeaderCell(56f, "DIAP"))
        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .addContent(rowBuilder.build())
            .build()
    }

    private fun MaterialScope.buildHeaderCell(
        widthDp: Float,
        label: String
    ): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Box.Builder()
            .setWidth(dp(widthDp))
            .setHeight(wrap())
            .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
            .addContent(
                text(
                    label.layoutString,
                    maxLines = 1,
                    typography = Typography.BODY_SMALL,
                    color = colorScheme.onSurfaceVariant
                )
            )
            .build()
    }

    private fun MaterialScope.buildCell(
        widthDp: Float,
        primary: String,
        secondary: String?,
        secondaryPillDt: Long?,
        alignCenter: Boolean,
        emphasize: Boolean
    ): LayoutElementBuilders.LayoutElement {
        val column = LayoutElementBuilders.Column.Builder()
            .setWidth(dp(widthDp))
            .setHeight(wrap())
            .setHorizontalAlignment(
                if (alignCenter) LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER
                else LayoutElementBuilders.HORIZONTAL_ALIGN_START
            )

        column.addContent(
            text(
                primary.layoutString,
                maxLines = if (emphasize) 2 else 1,
                typography = if (emphasize) Typography.TITLE_SMALL else Typography.BODY_MEDIUM
            )
        )
        if (secondary != null) {
            if (secondaryPillDt != null) {
                val colors = NaraGaidenFormat.timeColors(secondaryPillDt)
                column.addContent(
                    LayoutElementBuilders.Box.Builder()
                        .setWidth(wrap())
                        .setHeight(wrap())
                        .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                        .setModifiers(
                            ModifiersBuilders.Modifiers.Builder()
                                .setPadding(
                                    ModifiersBuilders.Padding.Builder()
                                        .setTop(dp(3f))
                                        .build()
                                )
                                .setBackground(
                                    ModifiersBuilders.Background.Builder()
                                        .setColor(argb(colors.bg))
                                        .setCorner(
                                            ModifiersBuilders.Corner.Builder()
                                                .setRadius(dp(8f))
                                                .build()
                                        )
                                        .build()
                                )
                                .build()
                        )
                        .addContent(
                            LayoutElementBuilders.Box.Builder()
                                .setWidth(wrap())
                                .setHeight(wrap())
                                .setHorizontalAlignment(LayoutElementBuilders.HORIZONTAL_ALIGN_CENTER)
                                .setModifiers(
                                    ModifiersBuilders.Modifiers.Builder()
                                        .setPadding(
                                            ModifiersBuilders.Padding.Builder()
                                                .setStart(dp(2f))
                                                .setEnd(dp(2f))
                                                .setTop(dp(2f))
                                                .setBottom(dp(2f))
                                                .build()
                                        )
                                        .build()
                                )
                                .addContent(
                                    text(
                                        secondary.layoutString,
                                        maxLines = 1,
                                        typography = Typography.BODY_MEDIUM
                                    )
                                )
                                .build()
                        )
                        .build()
                )
            } else {
                column.addContent(
                    text(
                        secondary.layoutString,
                        maxLines = 1,
                        typography = Typography.BODY_MEDIUM,
                        color = colorScheme.onSurfaceVariant
                    )
                )
            }
        }
        return column.build()
    }

    private fun MaterialScope.buildRowSeparator(): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Box.Builder()
            .setWidth(expand())
            .setHeight(dp(1f))
            .setModifiers(
                ModifiersBuilders.Modifiers.Builder()
                    .setBackground(
                        ModifiersBuilders.Background.Builder()
                            .setColor(argb(0xFF2A2A2A.toInt()))
                            .build()
                    )
                    .build()
            )
            .build()
    }

    private fun buildHorizontalSpacer(widthDp: Float): LayoutElementBuilders.LayoutElement {
        return LayoutElementBuilders.Spacer.Builder()
            .setWidth(dp(widthDp))
            .setHeight(dp(1f))
            .build()
    }

    companion object {
        private const val FRESHNESS_INTERVAL_MS = 5 * 60 * 1000L
        private const val REFRESH_CLICK_ID = "refresh"
        private const val BOTTOM_REFRESH_CLICK_ID = "bottom_refresh"
    }
}
