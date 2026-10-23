package org.nous.phonebridge

import android.content.Context

object ScreenControl {
    private const val PREFS = "screen_control"
    private const val UNTIL = "until"
    const val UNTIL_STOPPED = Long.MAX_VALUE

    fun remaining(until: Long, now: Long): Long? = when {
        until == UNTIL_STOPPED -> UNTIL_STOPPED
        until > now -> until - now
        else -> null
    }

    fun deadline(minutes: Int?, now: Long): Long = if (minutes == null) UNTIL_STOPPED else now + minutes * 60000L

    private fun prefs(context: Context) = context.getSharedPreferences(PREFS, Context.MODE_PRIVATE)

    fun remaining(context: Context): Long? = remaining(prefs(context).getLong(UNTIL, 0L), System.currentTimeMillis())

    fun enabled(context: Context): Boolean = remaining(context) != null

    fun enable(context: Context, minutes: Int?) {
        prefs(context).edit().putLong(UNTIL, deadline(minutes, System.currentTimeMillis())).commit()
        BridgeService.refresh()
    }

    fun stop(context: Context) {
        prefs(context).edit().putLong(UNTIL, 0L).commit()
        BridgeService.refresh()
    }

    fun summary(left: Long?): String? = when (left) {
        null -> null
        UNTIL_STOPPED -> "Screen control: ON until stopped"
        else -> "Screen control: ON, ${(left + 59999) / 60000} min left"
    }
}
