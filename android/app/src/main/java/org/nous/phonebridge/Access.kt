package org.nous.phonebridge

import android.Manifest
import android.app.KeyguardManager
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.os.PowerManager
import androidx.core.app.NotificationManagerCompat
import org.json.JSONArray
import org.json.JSONObject

object Access {
    fun has(context: Context, permission: String) = context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    fun need(context: Context, permission: String) {
        if (!has(context, permission)) failure("PERMISSION_REQUIRED", "Grant $permission in PhoneBridge settings")
    }
    fun unlocked(context: Context) {
        if (context.getSystemService(KeyguardManager::class.java).isKeyguardLocked || !context.getSystemService(PowerManager::class.java).isInteractive) failure("PHONE_LOCKED", "Unlock and wake the phone")
    }
    fun foreground(context: Context) {
        unlocked(context)
        if (!MainActivity.resumed && BridgeAccessibility.current == null) failure("FOREGROUND_REQUIRED", "Open PhoneBridge or enable Accessibility for foreground actions")
    }
    fun permissions(context: Context): JSONObject = JSONObject().apply {
        put("camera", has(context, Manifest.permission.CAMERA))
        put("contacts", has(context, Manifest.permission.READ_CONTACTS))
        put("calendar_read", has(context, Manifest.permission.READ_CALENDAR))
        put("calendar_write", has(context, Manifest.permission.WRITE_CALENDAR))
        put("sms_read", has(context, Manifest.permission.READ_SMS))
        put("sms_send", has(context, Manifest.permission.SEND_SMS))
        put("location", has(context, Manifest.permission.ACCESS_COARSE_LOCATION) || has(context, Manifest.permission.ACCESS_FINE_LOCATION))
        put("post_notifications", Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.POST_NOTIFICATIONS))
        put("notification_listener", NotificationManagerCompat.getEnabledListenerPackages(context).contains(context.packageName) && BridgeNotifications.current != null)
        put("notification_policy", context.getSystemService(android.app.NotificationManager::class.java).isNotificationPolicyAccessGranted)
        put("accessibility", BridgeAccessibility.current != null)
        put("foreground", MainActivity.resumed)
    }
    fun capabilities(context: Context): JSONArray {
        val ops = mutableSetOf("device.info", "capabilities", "device.capabilities", "apps.list", "audio.get", "audio.set", "clock.providers", "debug.status")
        val foreground = MainActivity.resumed || BridgeAccessibility.current != null
        if (foreground) {
            ops += listOf("apps.open", "calls.dial")
            ops += NativeClocks(context).nativeCapabilities()
        }
        if (MainActivity.resumed) ops += listOf("clipboard.get", "clipboard.set")
        if (has(context, Manifest.permission.READ_CONTACTS)) ops += "contacts.list"
        if (has(context, Manifest.permission.READ_CALENDAR)) ops += "calendar.list"
        if (has(context, Manifest.permission.WRITE_CALENDAR) && has(context, Manifest.permission.READ_CALENDAR)) ops += listOf("calendar.create", "calendar.delete")
        if (has(context, Manifest.permission.READ_SMS)) ops += "sms.list"
        if (has(context, Manifest.permission.SEND_SMS) && context.packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY_MESSAGING)) ops += "sms.send"
        if (MainActivity.resumed && (has(context, Manifest.permission.ACCESS_COARSE_LOCATION) || has(context, Manifest.permission.ACCESS_FINE_LOCATION))) ops += "location.get"
        if (has(context, Manifest.permission.CAMERA) && context.packageManager.hasSystemFeature(PackageManager.FEATURE_CAMERA_FLASH)) ops += "torch.set"
        if (BridgeNotifications.current != null) ops += listOf("notifications.list", "notifications.dismiss", "notifications.reply", "notifications.action")
        if (BridgeAccessibility.current != null) {
            if (DebugAccess.snapshot().enabled) {
                ops += listOf("screen.tree", "screen.tap", "screen.swipe", "screen.input", "screen.global")
                if (Build.VERSION.SDK_INT >= 34) ops += "screen.screenshot"
            }
            ops += ClockAdapters(context).capabilities()
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
