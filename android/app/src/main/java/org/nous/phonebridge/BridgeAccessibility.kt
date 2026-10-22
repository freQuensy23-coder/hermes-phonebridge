package org.nous.phonebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.Intent
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Bundle
import android.util.Base64
import android.view.Display
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

class BridgeAccessibility : AccessibilityService() {
    override fun onServiceConnected() {
        current = this
        BridgeService.refresh()
    }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) = Unit
    override fun onInterrupt() = Unit
    override fun onUnbind(intent: Intent?): Boolean {
        if (current === this) current = null
        BridgeService.refresh()
        return super.onUnbind(intent)
    }
    override fun onDestroy() {
        if (current === this) current = null
        BridgeService.refresh()
        super.onDestroy()
    }
    private fun root(): AccessibilityNodeInfo = rootInActiveWindow ?: failure("NOT_FOUND", "No window is on screen; wake and unlock the phone")
    private fun nodes(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = ArrayList<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 64 || result.size >= 3000) return
            result.add(node)
            for (i in 0 until node.childCount) node.getChild(i)?.let { visit(it, depth + 1) }
        }
        visit(root, 0)
        return result
    }
    fun handle(op: String, args: Arguments): JSONObject = when (op) {
        "screen.tree" -> { args.only(); UiThread.call { tree() } }
        "screen.screenshot" -> { args.only(); screenshot() }
        "screen.tap" -> {
            args.only("x", "y")
            val path = Path().apply { moveTo(args.int("x", 0, 100000).toFloat(), args.int("y", 0, 100000).toFloat()) }
            gesture(path, 70)
        }
        "screen.swipe" -> {
            args.only("x1", "y1", "x2", "y2", "duration_ms")
            val path = Path().apply {
                moveTo(args.int("x1", 0, 100000).toFloat(), args.int("y1", 0, 100000).toFloat())
                lineTo(args.int("x2", 0, 100000).toFloat(), args.int("y2", 0, 100000).toFloat())
            }
            gesture(path, args.int("duration_ms", 50, 5000, 400).toLong())
        }
        "screen.input" -> { args.only("resource_id", "text"); input(args.string("resource_id", 256, false), args.string("text", 4096)!!) }
        "screen.global" -> {
            args.only("action")
            val action = when (args.string("action", 32)) {
                "back" -> GLOBAL_ACTION_BACK
                "home" -> GLOBAL_ACTION_HOME
                "recents" -> GLOBAL_ACTION_RECENTS
                "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
                else -> failure("VALIDATION_ERROR", "action must be back, home, recents, notifications or quick_settings")
            }
            if (!UiThread.call { performGlobalAction(action) }) failure("UNSUPPORTED", "Android refused the global action")
            JSONObject().put("outcome", "dispatched").put("verified", false)
        }
        else -> failure("UNSUPPORTED", "Unknown screen operation $op")
    }
    private fun tree(): JSONObject {
        val root = root()
        val all = nodes(root)
        val informative = all.filter { node ->
            node.isVisibleToUser && (!node.text.isNullOrBlank() || !node.contentDescription.isNullOrBlank() || node.isClickable || node.isLongClickable || node.isEditable || node.isCheckable || node.isScrollable || node.isFocused)
        }
        val result = JSONArray()
        var bytes = 0
        for (node in informative) {
            val bounds = Rect().also { node.getBoundsInScreen(it) }
            val value = JSONObject().put("resource_id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString() ?: "").put("text", node.text?.toString()?.take(256) ?: "").put("description", node.contentDescription?.toString()?.take(256) ?: "").put("clickable", node.isClickable).put("editable", node.isEditable).put("scrollable", node.isScrollable).put("focused", node.isFocused).put("checked", node.isChecked).put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
            val size = value.toString().toByteArray().size
            if (result.length() >= TREE_NODES || bytes + size > TREE_BYTES) break
            bytes += size
            result.put(value)
        }
        return JSONObject().put("package", root.packageName?.toString() ?: JSONObject.NULL).put("nodes", result).put("truncated", result.length() < informative.size || all.size >= 3000).put("untrusted", true)
    }
    private fun input(resourceId: String?, text: String): JSONObject {
        val node = UiThread.call {
            val root = root()
            if (resourceId == null) root.findFocus(AccessibilityNodeInfo.FOCUS_INPUT) ?: failure("NOT_FOUND", "No text field has focus; tap the field first or pass resource_id")
            else nodes(root).filter { it.viewIdResourceName == resourceId && it.isEditable }.let { matches -> matches.firstOrNull { it.isFocused } ?: matches.firstOrNull() } ?: failure("NOT_FOUND", "No editable field with resource_id $resourceId on screen")
        }
        val accepted = UiThread.call { node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) }) }
        if (!accepted) failure("UNSUPPORTED", "This field does not accept text from Accessibility; tap the keyboard keys instead")
        var verified = false
        for (attempt in 0 until 10) {
            verified = UiThread.call { node.refresh(); node.text?.toString() == text }
            if (verified) break
            Thread.sleep(100)
        }
        return JSONObject().put("outcome", if (verified) "changed" else "dispatched").put("verified", verified)
    }
    private fun gesture(path: Path, duration: Long): JSONObject {
        val completed = CountDownLatch(1)
        var finished = false
        val gesture = GestureDescription.Builder().setDisplayId(Display.DEFAULT_DISPLAY).addStroke(GestureDescription.StrokeDescription(path, 0, duration)).build()
        val dispatched = UiThread.call {
            dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) { finished = true; completed.countDown() }
                override fun onCancelled(gestureDescription: GestureDescription?) { completed.countDown() }
            }, null)
        }
        if (!dispatched) failure("UNSUPPORTED", "Android refused the gesture")
        completed.await(duration + 3000, TimeUnit.MILLISECONDS)
        return JSONObject().put("outcome", "dispatched").put("gesture_completed", finished).put("verified", false)
    }
    private fun screenshot(): JSONObject {
        val latch = CountDownLatch(1)
        var image: JSONObject? = null
        var error: BridgeFailure? = null
        val pkg = rootInActiveWindow?.packageName?.toString()
        UiThread.call {
            takeScreenshot(Display.DEFAULT_DISPLAY, mainExecutor, object : TakeScreenshotCallback {
                override fun onSuccess(result: ScreenshotResult) {
                    val buffer = result.hardwareBuffer
                    try {
                        val hardware = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: failure("UNSUPPORTED", "Cannot decode screenshot")
                        val software = try { hardware.copy(Bitmap.Config.ARGB_8888, false) ?: failure("UNSUPPORTED", "Cannot copy screenshot") } finally { hardware.recycle() }
                        image = try { encodeScreenshot(software, pkg) } finally { software.recycle() }
                    } catch (e: BridgeFailure) { error = e } catch (_: Exception) { error = BridgeFailure("UNSUPPORTED", "Screenshot conversion failed") } finally { buffer.close(); latch.countDown() }
                }
                override fun onFailure(errorCode: Int) {
                    error = BridgeFailure("UNSUPPORTED", if (errorCode == ERROR_TAKE_SCREENSHOT_INTERVAL_TIME_SHORT) "Screenshots are limited to one per second; try again" else "Android refused the screenshot (code $errorCode)")
                    latch.countDown()
                }
            })
        }
        if (!latch.await(8, TimeUnit.SECONDS)) failure("AMBIGUOUS_STATE", "Screenshot timed out")
        error?.let { throw it }
        return image ?: failure("UNSUPPORTED", "No screenshot")
    }
    private fun encodeScreenshot(original: Bitmap, pkg: String?): JSONObject {
        val scale = minOf(1.0, 1280.0 / maxOf(original.width, original.height))
        val bitmap = Bitmap.createScaledBitmap(original, (original.width * scale).toInt().coerceAtLeast(1), (original.height * scale).toInt().coerceAtLeast(1), true)
        try {
            var bytes: ByteArray? = null
            for (quality in listOf(85, 75, 65, 55, 45, 35)) {
                val output = ByteArrayOutputStream()
                if (!bitmap.compress(Bitmap.CompressFormat.JPEG, quality, output)) failure("UNSUPPORTED", "JPEG encoder refused screenshot")
                if (output.size() <= PayloadBounds.SCREENSHOT_JPEG_BYTES) { bytes = output.toByteArray(); break }
            }
            val jpeg = bytes ?: failure("UNSUPPORTED", "Screenshot does not fit the size limit")
            val screen = JSONArray(listOf(0, 0, original.width, original.height))
            return JSONObject().put("mime", "image/jpeg").put("base64", Base64.encodeToString(jpeg, Base64.NO_WRAP)).put("width", bitmap.width).put("height", bitmap.height).put("screen_bounds", screen).put("window_bounds", screen).put("package", pkg ?: JSONObject.NULL)
        } finally { if (bitmap !== original) bitmap.recycle() }
    }
    companion object {
        const val TREE_NODES = 100
        const val TREE_BYTES = 60000
        @Volatile var current: BridgeAccessibility? = null
    }
}
