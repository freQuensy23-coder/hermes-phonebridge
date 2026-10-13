package org.nous.phonebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.util.Base64
import android.view.accessibility.AccessibilityEvent
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.io.ByteArrayOutputStream
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicLong

class BridgeAccessibility : AccessibilityService() {
    val revision = AtomicLong(0)
    override fun onServiceConnected() {
        current = this
        BridgeService.refresh()
    }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) { revision.incrementAndGet() }
    override fun onInterrupt() { revision.incrementAndGet() }
    override fun onDestroy() {
        if (current === this) current = null
        BridgeService.refresh()
        super.onDestroy()
    }
    fun root(expected: String? = null): AccessibilityNodeInfo {
        Access.unlocked(this)
        val root = rootInActiveWindow ?: failure("FOREGROUND_REQUIRED", "No accessible active window")
        val pkg = root.packageName?.toString() ?: failure("UNSUPPORTED", "Unknown active package")
        if (pkg in blockedPackages) failure("UNSUPPORTED", "Credential and lock screens are excluded")
        if (expected != null && pkg != expected) failure("FOREGROUND_REQUIRED", "Active package changed; expected $expected")
        return root
    }
    fun nodes(root: AccessibilityNodeInfo, includePasswords: Boolean = false): List<AccessibilityNodeInfo> {
        val result = ArrayList<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 30 || result.size >= 400) return
            if (node.isPassword && !includePasswords) return
            result.add(node)
            for (i in 0 until node.childCount.coerceAtMost(100)) node.getChild(i)?.let { visit(it, depth + 1) }
        }
        visit(root, 0)
        return result
    }
    fun safeRoot(expected: String): AccessibilityNodeInfo {
        val root = root(expected)
        assertSafeWindow(root)
        return root
    }
    private fun assertSafeWindow(root: AccessibilityNodeInfo) {
        var count = 0
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 30 || ++count > 400 || node.childCount > 100) failure("UNSUPPORTED", "Window exceeds complete password-guard traversal bound")
            if (node.isPassword) failure("UNSUPPORTED", "Input and gestures on password-bearing windows are excluded")
            for (index in 0 until node.childCount) node.getChild(index)?.let { visit(it, depth + 1) }
        }
        visit(root, 0)
    }
    fun exact(root: AccessibilityNodeInfo, id: String, text: String? = null): AccessibilityNodeInfo {
        val matches = nodes(root).filter { it.viewIdResourceName == id && (text == null || it.text?.toString() == text) && it.isVisibleToUser }
        if (matches.size != 1) failure("UNSUPPORTED", "Selector must resolve to exactly one visible node")
        return matches.single()
    }
    fun click(node: AccessibilityNodeInfo) {
        if (node.isPassword || !node.isVisibleToUser || !node.isEnabled || !node.isClickable) failure("UNSUPPORTED", "Node is not safely clickable")
        if (!node.performAction(AccessibilityNodeInfo.ACTION_CLICK)) failure("UNSUPPORTED", "Accessibility click refused")
    }
    fun handle(op: String, args: Arguments): JSONObject {
        return when (op) {
            "screen.tree" -> {
                args.only("expected_package")
                UiThread.call {
                    val root = root(args.string("expected_package", 256, false))
                    val result = JSONArray()
                    for (node in nodes(root)) {
                        val bounds = Rect().also { node.getBoundsInScreen(it) }
                        val value = JSONObject().put("resource_id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString() ?: "").put("text", node.text?.toString()?.take(256) ?: "").put("description", node.contentDescription?.toString()?.take(256) ?: "").put("clickable", node.isClickable).put("editable", node.isEditable).put("checked", node.isChecked).put("enabled", node.isEnabled).put("visible", node.isVisibleToUser).put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                        if (result.length() >= 48 || result.toString().toByteArray().size + value.toString().toByteArray().size > 24000) break
                        result.put(value)
                    }
                    JSONObject().put("package", root.packageName).put("revision", revision.get()).put("nodes", result).put("bounded", true).put("password_nodes_excluded", true).put("untrusted", true)
                }
            }
            "screen.input" -> {
                args.only("expected_package", "resource_id", "expected_text", "text")
                val pkg = args.string("expected_package", 256)!!
                val id = args.string("resource_id", 256)!!
                val text = args.string("text", 4096)!!
                UiThread.call {
                    val node = exact(safeRoot(pkg), id, args.string("expected_text", 4096, false))
                    if (!node.isEditable || !node.isEnabled) failure("UNSUPPORTED", "Node does not accept text")
                    val accepted = node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) })
                    if (!accepted) failure("UNSUPPORTED", "Text action refused")
                }
                var verified = false
                for (attempt in 0 until 10) {
                    verified = UiThread.call { exact(safeRoot(pkg), id).text?.toString() == text }
                    if (verified) break
                    Thread.sleep(100)
                }
                JSONObject().put("outcome", if (verified) "changed" else "dispatched").put("verified", verified)
            }
            "screen.tap", "screen.swipe" -> gesture(op, args)
            "screen.global" -> {
                args.only("action")
                val action = when (args.string("action", 32)) {
                    "back" -> GLOBAL_ACTION_BACK
                    "home" -> GLOBAL_ACTION_HOME
                    "recents" -> GLOBAL_ACTION_RECENTS
                    "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                    "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
                    else -> failure("VALIDATION_ERROR", "Global action is not allowlisted")
                }
                UiThread.call {
                    val root = root()
                    assertSafeWindow(root)
                    if (!performGlobalAction(action)) failure("UNSUPPORTED", "Global action refused")
                    JSONObject().put("outcome", "dispatched").put("verified", false)
                }
            }
            "screen.screenshot" -> screenshot(args)
            else -> failure("UNSUPPORTED", "Unknown screen operation")
        }
    }
    private fun gesture(op: String, args: Arguments): JSONObject {
        args.only("expected_package", "x", "y", "x1", "y1", "x2", "y2", "duration_ms")
        val pkg = args.string("expected_package", 256)!!
        val completed = CountDownLatch(1)
        var accepted = false
        UiThread.call {
            val root = safeRoot(pkg)
            val bounds = Rect().also { root.getBoundsInScreen(it) }
            requireBridge(bounds.width() > 0 && bounds.height() > 0, "Empty screen bounds")
            val path = Path()
            val duration: Int
            if (op == "screen.tap") {
                path.moveTo(args.int("x", bounds.left, bounds.right - 1).toFloat(), args.int("y", bounds.top, bounds.bottom - 1).toFloat())
                duration = 70
            } else {
                path.moveTo(args.int("x1", bounds.left, bounds.right - 1).toFloat(), args.int("y1", bounds.top, bounds.bottom - 1).toFloat())
                path.lineTo(args.int("x2", bounds.left, bounds.right - 1).toFloat(), args.int("y2", bounds.top, bounds.bottom - 1).toFloat())
                duration = args.int("duration_ms", 100, 2000, 400)
            }
            val gesture = GestureDescription.Builder().addStroke(GestureDescription.StrokeDescription(path, 0, duration.toLong())).build()
            if (!dispatchGesture(gesture, object : GestureResultCallback() {
                override fun onCompleted(gestureDescription: GestureDescription?) { accepted = true; completed.countDown() }
                override fun onCancelled(gestureDescription: GestureDescription?) { completed.countDown() }
            }, null)) failure("UNSUPPORTED", "Gesture dispatch refused")
        }
        if (!completed.await(4, TimeUnit.SECONDS)) failure("AMBIGUOUS_STATE", "Gesture callback timed out; no retry")
        return JSONObject().put("outcome", "dispatched").put("gesture_completed", accepted).put("verified", false)
    }
    private fun screenshot(args: Arguments): JSONObject {
        args.only("expected_package")
        if (Build.VERSION.SDK_INT < 34) failure("UNSUPPORTED", "Secure-window-aware screenshots require Android 14 or newer")
        val expected = args.string("expected_package", 256)!!
        val latch = CountDownLatch(1)
        var image: JSONObject? = null
        var error: BridgeFailure? = null
        UiThread.call {
            val root = safeRoot(expected)
            val window = windows.singleOrNull { it.id == root.windowId && it.isActive } ?: failure("UNSUPPORTED", "No unique active window")
            val initialRevision = revision.get()
            takeScreenshotOfWindow(window.id, mainExecutor, object : TakeScreenshotCallback {
                override fun onSuccess(result: ScreenshotResult) {
                    val buffer = result.hardwareBuffer
                    try {
                        Access.unlocked(this@BridgeAccessibility)
                        if (revision.get() != initialRevision || rootInActiveWindow?.packageName?.toString() != expected) failure("AMBIGUOUS_STATE", "Screen changed during capture; image discarded")
                        val original = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: failure("UNSUPPORTED", "Cannot decode screenshot")
                        try {
                            val scale = minOf(1.0, 240.0 / maxOf(original.width, original.height))
                            val bitmap = Bitmap.createScaledBitmap(original, (original.width * scale).toInt().coerceAtLeast(1), (original.height * scale).toInt().coerceAtLeast(1), true)
                            try {
                                val bytes = ByteArrayOutputStream()
                                bitmap.compress(Bitmap.CompressFormat.JPEG, 35, bytes)
                                if (bytes.size() > 10000) failure("UNSUPPORTED", "Screenshot exceeds bounded payload")
                                image = JSONObject().put("mime", "image/jpeg").put("base64", Base64.encodeToString(bytes.toByteArray(), Base64.NO_WRAP)).put("width", bitmap.width).put("height", bitmap.height).put("package", expected).put("secure_window_guard", "platform_window_capture")
                            } finally { if (bitmap !== original) bitmap.recycle() }
                        } finally { original.recycle() }
                    } catch (e: BridgeFailure) { error = e } catch (_: Exception) { error = BridgeFailure("UNSUPPORTED", "Screenshot conversion unavailable") } finally { buffer.close(); latch.countDown() }
                }
                override fun onFailure(errorCode: Int) {
                    error = BridgeFailure("UNSUPPORTED", "Platform refused window screenshot ($errorCode); secure windows are excluded")
                    latch.countDown()
                }
            })
        }
        if (!latch.await(8, TimeUnit.SECONDS)) failure("AMBIGUOUS_STATE", "Screenshot timed out")
        error?.let { throw it }
        return image ?: failure("UNSUPPORTED", "No screenshot")
    }
    companion object {
        @Volatile var current: BridgeAccessibility? = null
        val blockedPackages = setOf("com.android.systemui", "com.android.settings", "com.google.android.gms", "com.samsung.android.biometrics.app.setting")
    }
}
