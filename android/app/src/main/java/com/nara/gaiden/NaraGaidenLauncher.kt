package com.nara.gaiden

import android.content.Context
import android.content.Intent
import android.net.Uri

object NaraGaidenLauncher {
    private const val NARA_PACKAGE = "com.naraorganics.nara"

    fun launchGaidenApp(context: Context) {
        val launch = Intent(context, MainActivity::class.java).apply {
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(launch)
        } catch (_: Exception) {
            return
        }
    }

    fun launchNaraApp(context: Context) {
        val pm = context.packageManager
        val launch = pm.getLaunchIntentForPackage(NARA_PACKAGE)
        if (launch != null) {
            launch.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
            try {
                context.startActivity(launch)
                return
            } catch (_: Exception) {
                return
            }
        }

        val storeIntent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("market://details?id=$NARA_PACKAGE")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(storeIntent)
            return
        } catch (_: Exception) {
        }

        val webIntent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse("https://play.google.com/store/apps/details?id=$NARA_PACKAGE")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(webIntent)
        } catch (_: Exception) {
            return
        }
    }

    fun launchPlots(context: Context) {
        val plotIntent = Intent(Intent.ACTION_VIEW).apply {
            data = Uri.parse(NaraGaidenConfig.plotUrl)
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        try {
            context.startActivity(plotIntent)
        } catch (_: Exception) {
            return
        }
    }
}
