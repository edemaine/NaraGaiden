package com.nara.gaiden

object NaraGaidenConfig {
    val serverUrl = BuildConfig.NARA_GAIDEN_SERVER_URL

    val jsonUrl: String
        get() = "$serverUrl/json"

    val plotUrl: String
        get() = "$serverUrl/plot"
}
