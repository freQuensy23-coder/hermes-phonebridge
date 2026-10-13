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
        put("accessibility", BridgeAccessibility.current != null)
        put("foreground", MainActivity.resumed)
    }
    fun capabilities(context: Context): JSONArray {
        val ops = mutableSetOf("device.info", "device.capabilities", "apps.list", "audio.get", "audio.set", "clock.providers")
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
            ops += listOf("screen.tree", "screen.tap", "screen.swipe", "screen.input", "screen.global")
            if (Build.VERSION.SDK_INT >= 34) ops += "screen.screenshot"
            ops += ClockAdapters(context).capabilities()
        }
        return JSONArray(ops.sorted())
    }
}
