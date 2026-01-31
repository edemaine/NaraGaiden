package com.nara.gaiden

import android.content.Intent
import android.os.Bundle
import android.widget.Button
import android.widget.TextView
import androidx.appcompat.app.AppCompatActivity

class MainActivity : AppCompatActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val serverView = findViewById<TextView>(R.id.server_url)
        serverView.text = NaraGaidenConfig.serverUrl

        val refreshButton = findViewById<Button>(R.id.refresh_button)
        refreshButton.setOnClickListener {
            val intent = Intent(this, NaraGaidenWidgetProvider::class.java).apply {
                action = NaraGaidenWidgetProvider.ACTION_REFRESH
            }
            sendBroadcast(intent)
        }
    }
}
