package org.nous.phonebridge

import android.Manifest
import android.app.KeyguardManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import org.json.JSONArray
import org.json.JSONObject

object Access {
    fun has(context: Context, permission: String) = context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    fun unlocked(context: Context) {
        if (context.getSystemService(KeyguardManager::class.java).isKeyguardLocked || !context.getSystemService(PowerManager::class.java).isInteractive) failure("PHONE_LOCKED", "Unlock and wake the phone")
    }
    fun permissions(context: Context): JSONObject = JSONObject().apply {
        put("post_notifications", Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.POST_NOTIFICATIONS))
        put("accessibility", BridgeAccessibility.current != null)
        put("screen_control", DebugAccess.snapshot().enabled)
    }
    fun capabilities(context: Context): JSONArray {
        val ops = mutableSetOf("device.info", "capabilities", "device.capabilities", "debug.status", "files.receive")
        if (BridgeAccessibility.current != null && DebugAccess.snapshot().enabled) {
            ops += listOf("screen.tree", "screen.tap", "screen.swipe", "screen.input", "screen.global")
            if (Build.VERSION.SDK_INT >= 34) ops += "screen.screenshot"
        }
        return JSONArray(ops.sorted())
    }
}

object DebugArguments {
    fun session(args: Arguments): String {
        if (!args.json.has("debug_session") || args.json.isNull("debug_session")) failure("PERMISSION_REQUIRED", "Current debug_session required")
        return args.string("debug_session", 128)!!
    }
}

internal object DebugAccess {
    private val control = DebugControl { android.os.SystemClock.elapsedRealtime() }
    private val handler = android.os.Handler(android.os.Looper.getMainLooper())
    private val ticker = object : Runnable {
        override fun run() {
            val state = snapshot()
            BridgeService.refresh()
            if (state.enabled) handler.postDelayed(this, 1000)
        }
    }

    fun snapshot(): DebugSnapshot = control.status()

    fun status(): JSONObject {
        val state = snapshot()
        return JSONObject().put("enabled", state.enabled).put("experimental", true)
            .put("remaining_seconds", state.remainingSeconds ?: JSONObject.NULL)
            .put("debug_session", state.debugSession ?: JSONObject.NULL)
            .put("screenshot_supported", Build.VERSION.SDK_INT >= 34 && BridgeAccessibility.current != null)
    }

    fun enableLocal(activity: MainActivity, minutes: Int?) {
        if (!MainActivity.resumed || !activity.window.decorView.isShown || activity.isFinishing || activity.isDestroyed) failure("FOREGROUND_REQUIRED", "Use the visible local confirmation")
        Access.unlocked(activity)
        if (BridgeAccessibility.current == null) failure("PERMISSION_REQUIRED", "Grant Accessibility in Android settings first")
        if (BridgeService.current?.debugConnectionReady() != true) failure("PERMISSION_REQUIRED", "Connect PhoneBridge before enabling debug")
        if (!androidx.core.app.NotificationManagerCompat.from(activity).areNotificationsEnabled()) failure("PERMISSION_REQUIRED", "Allow the visible notification and its Stop debug action first")
        if (activity.getSystemService(android.app.NotificationManager::class.java).getNotificationChannel(BridgeService.CHANNEL)?.importance == android.app.NotificationManager.IMPORTANCE_NONE) failure("PERMISSION_REQUIRED", "Enable the PhoneBridge notification channel first")
        control.enableLocal(minutes)
        handler.removeCallbacks(ticker)
        handler.post(ticker)
        BridgeService.refresh()
    }

    fun revoke() {
        control.revoke()
        handler.removeCallbacks(ticker)
        BridgeService.refresh()
    }

    fun check(session: String) = withSession(session) { }

    fun <T> withSession(session: String, block: () -> T): T = control.withSession(session) {
        if (BridgeAccessibility.current == null || BridgeService.current?.debugConnectionReady() != true) {
            control.revoke()
            failure("PERMISSION_REQUIRED", "Debug connection or Accessibility ended; enable again locally")
        }
        block()
    }

    fun flight(session: String): DebugFlight = control.withSession(session) { DebugFlight(control, session) }
}
