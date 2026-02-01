package com.nara.gaiden

import org.json.JSONObject
import java.io.IOException
import java.net.HttpURLConnection
import java.net.URL
import java.text.DateFormat
import java.util.Date

data class NaraGaidenWidgetState(
    val statusLine: String,
    val updatedLine: String
) {
    companion object {
        fun loading(lastUpdated: String?): NaraGaidenWidgetState {
            return NaraGaidenWidgetState(
                statusLine = "Loading...",
                updatedLine = lastUpdated ?: "Updated: --"
            )
        }

        fun error(message: String, lastUpdated: String?): NaraGaidenWidgetState {
            return NaraGaidenWidgetState(
                statusLine = "Error: $message",
                updatedLine = lastUpdated ?: "Updated: --"
            )
        }

        fun idle(lastUpdated: String?): NaraGaidenWidgetState {
            return NaraGaidenWidgetState(
                statusLine = "",
                updatedLine = lastUpdated ?: "Updated: --"
            )
        }

        fun ready(updatedLine: String): NaraGaidenWidgetState {
            return NaraGaidenWidgetState(
                statusLine = "Nara Gaiden",
                updatedLine = updatedLine
            )
        }
    }
}

data class NaraGaidenFetchResult(
    val json: String,
    val updatedLine: String
)

object NaraGaidenApi {
    fun fetch(): NaraGaidenFetchResult {
        val url = URL(NaraGaidenConfig.serverUrl)
        val connection = url.openConnection() as HttpURLConnection
        connection.connectTimeout = 15000
        connection.readTimeout = 15000
        connection.requestMethod = "GET"
        connection.setRequestProperty("Accept", "application/json")

        val responseCode = connection.responseCode
        if (responseCode != 200) {
            throw IOException("HTTP $responseCode")
        }

        val body = connection.inputStream.bufferedReader().use { it.readText() }
        val json = JSONObject(body)
        val generatedAt = json.optLong("generatedAt", 0L)
        return NaraGaidenFetchResult(
            json = body,
            updatedLine = formatUpdated(generatedAt)
        )
    }

    private fun formatUpdated(generatedAt: Long): String {
        if (generatedAt <= 0) {
            return "Updated: unknown"
        }
        val formatted = DateFormat.getTimeInstance(DateFormat.SHORT).format(Date(generatedAt))
        return "Updated: $formatted"
    }
}
