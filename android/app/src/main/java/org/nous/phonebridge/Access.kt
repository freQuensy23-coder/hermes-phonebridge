package org.nous.phonebridge

import android.Manifest
import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import org.json.JSONArray
import org.json.JSONObject

object Access {
    fun has(context: Context, permission: String) = context.checkSelfPermission(permission) == PackageManager.PERMISSION_GRANTED
    fun permissions(context: Context): JSONObject = JSONObject().apply {
        put("post_notifications", Build.VERSION.SDK_INT < 33 || has(context, Manifest.permission.POST_NOTIFICATIONS))
        put("accessibility", BridgeAccessibility.current != null)
        put("screen_control", ScreenControl.enabled(context))
    }
    fun capabilities(context: Context): JSONArray {
        val ops = mutableSetOf("device.info", "capabilities", "device.capabilities", "files.receive")
        if (BridgeAccessibility.current != null && ScreenControl.enabled(context)) ops += listOf("screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global")
        return JSONArray(ops.sorted())
    }
}
