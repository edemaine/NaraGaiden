package com.nara.gaiden

object NaraGaidenConfig {
    const val serverUrl = "http://192.168.2.1:8888"

    val jsonUrl: String
        get() = "$serverUrl/json"

    val plotUrl: String
        get() = "$serverUrl/plot"
}
