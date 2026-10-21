package org.nous.phonebridge

import android.accessibilityservice.AccessibilityService
import android.accessibilityservice.GestureDescription
import android.content.ComponentName
import android.content.Intent
import android.content.pm.ApplicationInfo
import android.content.pm.PackageManager
import android.content.res.Resources
import android.graphics.Bitmap
import android.graphics.Path
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.Bundle
import android.util.Base64
import android.view.Display
import android.view.WindowInsets
import android.view.WindowManager
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
        DebugAccess.revoke()
        current = this
        BridgeService.refresh()
    }
    override fun onAccessibilityEvent(event: AccessibilityEvent?) { revision.incrementAndGet() }
    override fun onInterrupt() { revision.incrementAndGet(); DebugAccess.revoke() }
    override fun onUnbind(intent: Intent?): Boolean {
        if (current === this) current = null
        DebugAccess.revoke()
        return super.onUnbind(intent)
    }
    override fun onDestroy() {
        if (current === this) current = null
        DebugAccess.revoke()
        super.onDestroy()
    }
    fun root(expected: String? = null): AccessibilityNodeInfo {
        Access.unlocked(this)
        val root = rootInActiveWindow ?: failure("FOREGROUND_REQUIRED", "No accessible active window")
        val pkg = root.packageName?.toString() ?: failure("UNSUPPORTED", "Unknown active package")
        if (blocked(pkg)) failure("UNSUPPORTED", "PhoneBridge, permission, credential and lock screens are excluded")
        if (expected != null && pkg != expected) failure("FOREGROUND_REQUIRED", "Active package changed; expected $expected")
        return root
    }
    private val permissionControllerPackage by lazy {
        packageManager.resolveActivity(Intent("android.intent.action.MANAGE_PERMISSIONS"), android.content.pm.PackageManager.MATCH_SYSTEM_ONLY)?.activityInfo?.packageName
    }
    private fun blocked(pkg: String) = pkg == packageName || pkg == permissionControllerPackage || pkg in blockedPackages || pkg.endsWith(".permissioncontroller") || pkg.endsWith(".packageinstaller") || pkg.endsWith(".credentialmanager")
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
    private val remoteNodeAdapter = object : RemoteWindowNodeAdapter<AccessibilityNodeInfo> {
        override fun windowId(node: AccessibilityNodeInfo) = node.windowId
        override fun owner(node: AccessibilityNodeInfo) = node.packageName?.toString()
        override fun bounds(node: AccessibilityNodeInfo): RemoteWindowBounds {
            val bounds = Rect().also { node.getBoundsInScreen(it) }
            return RemoteWindowBounds(bounds.left, bounds.top, bounds.right, bounds.bottom)
        }
        override fun isPassword(node: AccessibilityNodeInfo) = node.isPassword
        override fun childCount(node: AccessibilityNodeInfo) = node.childCount
        override fun child(node: AccessibilityNodeInfo, index: Int) = node.getChild(index)
    }
    private fun assertSafeWindow(root: AccessibilityNodeInfo) = RemoteWindowPolicy.assertSafeWindow(root, remoteNodeAdapter)
    private fun verifiedNavigationOwner(): String? {
        return try {
            val resource = resources.getIdentifier("config_recentsComponentName", "string", "android")
            if (resource == 0) return null
            val component = ComponentName.unflattenFromString(resources.getString(resource)) ?: return null
            val activity = packageManager.getActivityInfo(component, PackageManager.MATCH_SYSTEM_ONLY)
            val application = activity.applicationInfo ?: return null
            if (activity.packageName != component.packageName || application.packageName != component.packageName) return null
            if (!activity.enabled || !application.enabled || application.flags and ApplicationInfo.FLAG_SYSTEM == 0) return null
            val enabledStates = setOf(PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, PackageManager.COMPONENT_ENABLED_STATE_ENABLED)
            if (packageManager.getApplicationEnabledSetting(component.packageName) !in enabledStates || packageManager.getComponentEnabledSetting(component) !in enabledStates) return null
            if (packageManager.checkPermission("android.permission.STATUS_BAR_SERVICE", component.packageName) != PackageManager.PERMISSION_GRANTED) return null
            component.packageName
        } catch (_: Resources.NotFoundException) {
            null
        } catch (_: PackageManager.NameNotFoundException) {
            null
        } catch (_: IllegalArgumentException) {
            null
        } catch (_: SecurityException) {
            null
        }
    }
    private fun nativeWindowGeometry(): RemoteWindowGeometry? {
        return try {
            val display = getSystemService(DisplayManager::class.java)?.getDisplay(Display.DEFAULT_DISPLAY) ?: return null
            if (!display.isValid) return null
            val context = createWindowContext(display, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY, null)
            if (context.display?.displayId != Display.DEFAULT_DISPLAY) return null
            val metrics = context.getSystemService(WindowManager::class.java)?.maximumWindowMetrics ?: return null
            val bounds = metrics.bounds
            fun insets(type: Int): RemoteWindowInsets {
                val value = metrics.windowInsets.getInsetsIgnoringVisibility(type)
                return RemoteWindowInsets(value.left, value.top, value.right, value.bottom)
            }
            RemoteWindowGeometry.fromInsets(
                RemoteWindowBounds(bounds.left, bounds.top, bounds.right, bounds.bottom),
                insets(WindowInsets.Type.statusBars()),
                insets(WindowInsets.Type.navigationBars()),
                insets(WindowInsets.Type.displayCutout())
            )
        } catch (_: Exception) { null }
    }
    private fun remoteRoot(expected: String? = null): RemoteWindowTarget<AccessibilityNodeInfo> {
        if (MainActivity.resumed) failure("UNSUPPORTED", "PhoneBridge consent/settings UI cannot be remotely controlled")
        val root = root(expected)
        val nativeWindows = windows
        val geometry = if (nativeWindows.all { it.displayId == Display.DEFAULT_DISPLAY }) nativeWindowGeometry() else null
        val snapshot = nativeWindows.map { window ->
            val bounds = Rect().also { window.getBoundsInScreen(it) }
            val kind = when (window.type) {
                android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION -> RemoteWindowKind.APPLICATION
                android.view.accessibility.AccessibilityWindowInfo.TYPE_SYSTEM -> RemoteWindowKind.SYSTEM
                else -> RemoteWindowKind.OTHER
            }
            RemoteWindowSnapshot(window.id, kind, window.isActive, window.isFocused, RemoteWindowBounds(bounds.left, bounds.top, bounds.right, bounds.bottom), window.root)
        }
        return RemoteWindowPolicy.resolve(root, expected, snapshot, remoteNodeAdapter, ::blocked, geometry, verifiedNavigationOwner())
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
    private fun <T> screenCall(session: String, block: () -> T): T = UiThread.call { DebugAccess.withSession(session, block) }
    private val debugSessions = object : DebugSessions {
        override fun <T> withSession(expected: String?, block: () -> T): T = DebugAccess.withSession(expected ?: "", block)
    }
    fun handle(op: String, args: Arguments): JSONObject {
        val session = DebugArguments.session(args)
        DebugAccess.check(session)
        val guard = ExecutionGuard.snapshot()
        val effects = ExecutionGuard.snapshotSinks(debugSessions, session) { Access.unlocked(this) }
        val result = when (op) {
            "screen.tree" -> {
                args.only("debug_session", "expected_package")
                screenCall(session) {
                    val root = remoteRoot(args.string("expected_package", 256, false)).root
                    val result = JSONArray()
                    for (node in nodes(root)) {
                        val bounds = Rect().also { node.getBoundsInScreen(it) }
                        val value = JSONObject().put("resource_id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString() ?: "").put("text", node.text?.toString()?.take(256) ?: "").put("description", node.contentDescription?.toString()?.take(256) ?: "").put("clickable", node.isClickable).put("editable", node.isEditable).put("checked", node.isChecked).put("enabled", node.isEnabled).put("visible", node.isVisibleToUser).put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                        if (result.length() >= 48 || result.toString().toByteArray().size + value.toString().toByteArray().size > 24000) break
                        result.put(value)
                    }
                    guard()
                    JSONObject().put("package", root.packageName).put("revision", revision.get()).put("nodes", result).put("bounded", true).put("password_nodes_excluded", true).put("untrusted", true)
                }
            }
            "screen.input" -> {
                args.only("debug_session", "expected_package", "resource_id", "expected_text", "text")
                val pkg = args.string("expected_package", 256)!!
                val id = args.string("resource_id", 256)!!
                val text = args.string("text", 4096)!!
                screenCall(session) {
                    val node = exact(remoteRoot(pkg).root, id, args.string("expected_text", 4096, false))
                    if (!node.isEditable || !node.isEnabled) failure("UNSUPPORTED", "Node does not accept text")
                    val accepted = effects.prepared({ Bundle().apply { putCharSequence(AccessibilityNodeInfo.ACTION_ARGUMENT_SET_TEXT_CHARSEQUENCE, text) } }) { arguments ->
                        node.performAction(AccessibilityNodeInfo.ACTION_SET_TEXT, arguments)
                    }
                    if (!accepted) failure("UNSUPPORTED", "Text action refused")
                }
                var verified = false
                for (attempt in 0 until 10) {
                    verified = screenCall(session) { exact(remoteRoot(pkg).root, id).text?.toString() == text }
                    if (verified) break
                    Thread.sleep(100)
                }
                JSONObject().put("outcome", if (verified) "changed" else "dispatched").put("verified", verified)
            }
            "screen.tap", "screen.swipe" -> gesture(op, args, session, guard, effects)
            "screen.global" -> {
                args.only("debug_session", "action")
                val action = when (args.string("action", 32)) {
                    "back" -> GLOBAL_ACTION_BACK
                    "home" -> GLOBAL_ACTION_HOME
                    "recents" -> GLOBAL_ACTION_RECENTS
                    "notifications" -> GLOBAL_ACTION_NOTIFICATIONS
                    "quick_settings" -> GLOBAL_ACTION_QUICK_SETTINGS
                    else -> failure("VALIDATION_ERROR", "Global action is not allowlisted")
                }
                screenCall(session) {
                    remoteRoot()
                    if (!effects.prepared({ action }) { performGlobalAction(it) }) failure("UNSUPPORTED", "Global action refused")
                    JSONObject().put("outcome", "dispatched").put("verified", false)
                }
            }
            "screen.screenshot" -> screenshot(args, session, guard, effects)
            else -> failure("UNSUPPORTED", "Unknown screen operation")
        }
        guard()
        return DebugAccess.withSession(session) { result }
    }
    private fun gesture(op: String, args: Arguments, session: String, guard: () -> Unit, effects: CommandSinks): JSONObject {
        args.only("debug_session", "expected_package", "x", "y", "x1", "y1", "x2", "y2", "duration_ms")
        val pkg = args.string("expected_package", 256)!!
        val completed = CountDownLatch(1)
        var accepted = false
        val flight = DebugAccess.flight(session)
        try {
            screenCall(session) {
                val target = remoteRoot(pkg)
                val bounds = Rect().also { target.root.getBoundsInScreen(it) }
                requireBridge(bounds.width() > 0 && bounds.height() > 0, "Empty screen bounds")
                if (!target.bounds.contains(RemoteWindowBounds(bounds.left, bounds.top, bounds.right, bounds.bottom))) failure("AMBIGUOUS_STATE", "Gesture root extends outside the selected native window")
                val path = Path()
                val duration: Int
                if (op == "screen.tap") {
                    val x = args.int("x", bounds.left, bounds.right - 1)
                    val y = args.int("y", bounds.top, bounds.bottom - 1)
                    target.requireGesturePoint(x, y)
                    path.moveTo(x.toFloat(), y.toFloat())
                    duration = 70
                } else {
                    val x1 = args.int("x1", bounds.left, bounds.right - 1)
                    val y1 = args.int("y1", bounds.top, bounds.bottom - 1)
                    val x2 = args.int("x2", bounds.left, bounds.right - 1)
                    val y2 = args.int("y2", bounds.top, bounds.bottom - 1)
                    target.requireGesturePoint(x1, y1)
                    target.requireGesturePoint(x2, y2)
                    path.moveTo(x1.toFloat(), y1.toFloat())
                    path.lineTo(x2.toFloat(), y2.toFloat())
                    duration = args.int("duration_ms", 100, 2000, 400)
                }
                val gesture = GestureDescription.Builder().setDisplayId(Display.DEFAULT_DISPLAY).addStroke(GestureDescription.StrokeDescription(path, 0, duration.toLong())).build()
                flight.effect {
                    val dispatched = effects.prepared({
                        object : GestureResultCallback() {
                            override fun onCompleted(gestureDescription: GestureDescription?) { accepted = true; completed.countDown() }
                            override fun onCancelled(gestureDescription: GestureDescription?) { completed.countDown() }
                        }
                    }) { callback -> dispatchGesture(gesture, callback, null) }
                    if (!dispatched) failure("UNSUPPORTED", "Gesture dispatch refused")
                }
            }
            if (!completed.await(4, TimeUnit.SECONDS)) failure("AMBIGUOUS_STATE", "Gesture callback timed out; no retry")
            guard()
            return DebugAccess.withSession(session) { JSONObject().put("outcome", "dispatched").put("gesture_completed", accepted).put("verified", false) }
        } finally { flight.cancel() }
    }
    private fun screenshot(args: Arguments, session: String, guard: () -> Unit, effects: CommandSinks): JSONObject {
        args.only("debug_session", "expected_package")
        if (Build.VERSION.SDK_INT < 34) failure("UNSUPPORTED", "Secure-window-aware screenshots require Android 14 or newer")
        val expected = args.string("expected_package", 256)!!
        val latch = CountDownLatch(1)
        val flight = DebugAccess.flight(session)
        var image: JSONObject? = null
        var error: BridgeFailure? = null
        try {
            screenCall(session) {
                val target = remoteRoot(expected)
                val bounds = Rect(target.bounds.left, target.bounds.top, target.bounds.right, target.bounds.bottom)
                val initialRevision = revision.get()
                val windowId = target.windowId
                val executor = mainExecutor
                flight.effect {
                    effects.prepared({ object : TakeScreenshotCallback {
                        override fun onSuccess(result: ScreenshotResult) {
                            val buffer = result.hardwareBuffer
                            try {
                                guard()
                                DebugAccess.withSession(session) {
                                    val fresh = remoteRoot(expected)
                                    if (revision.get() != initialRevision || fresh.windowId != target.windowId || fresh.owner != target.owner || fresh.bounds != target.bounds) failure("AMBIGUOUS_STATE", "Screen changed during capture; image discarded")
                                }
                                val hardware = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: failure("UNSUPPORTED", "Cannot decode screenshot")
                                val software = try { hardware.copy(Bitmap.Config.ARGB_8888, false) ?: failure("UNSUPPORTED", "Cannot copy screenshot") } finally { hardware.recycle() }
                                val encoded = try { encodeScreenshot(software, bounds, expected) } finally { software.recycle() }
                                guard()
                                DebugAccess.withSession(session) {
                                    val fresh = remoteRoot(expected)
                                    if (revision.get() != initialRevision || fresh.windowId != target.windowId || fresh.owner != target.owner || fresh.bounds != target.bounds) failure("AMBIGUOUS_STATE", "Screen changed during encoding; image discarded")
                                    flight.deliver { image = encoded }
                                }
                            } catch (e: BridgeFailure) { error = e } catch (_: Exception) { error = BridgeFailure("UNSUPPORTED", "Screenshot conversion unavailable") } finally { buffer.close(); latch.countDown() }
                        }
                        override fun onFailure(errorCode: Int) {
                            error = BridgeFailure("UNSUPPORTED", "Platform refused window screenshot ($errorCode); secure windows are excluded")
                            latch.countDown()
                        }
                    } }) { callback -> takeScreenshotOfWindow(windowId, executor, callback) }
                }
            }
            if (!latch.await(8, TimeUnit.SECONDS)) failure("AMBIGUOUS_STATE", "Screenshot timed out; no retry")
            guard()
            return DebugAccess.withSession(session) { error?.let { throw it }; image ?: failure("UNSUPPORTED", "No screenshot") }
        } finally { flight.cancel() }
    }
    private fun encodeScreenshot(original: Bitmap, bounds: Rect, expected: String): JSONObject {
        val scale = minOf(1.0, 1280.0 / maxOf(original.width, original.height))
        val bitmap = Bitmap.createScaledBitmap(original, (original.width * scale).toInt().coerceAtLeast(1), (original.height * scale).toInt().coerceAtLeast(1), true)
        try {
            var bytes: ByteArray? = null
            for (quality in listOf(85, 75, 65, 55, 45, 35)) {
                val output = ByteArrayOutputStream()
                if (!bitmap.compress(Bitmap.CompressFormat.JPEG, quality, output)) failure("UNSUPPORTED", "JPEG encoder refused screenshot")
                if (output.size() <= PayloadBounds.SCREENSHOT_JPEG_BYTES) { bytes = output.toByteArray(); break }
            }
            val jpeg = bytes ?: failure("UNSUPPORTED", "Readable screenshot cannot fit the bounded JPEG budget")
            return JSONObject().put("mime", "image/jpeg").put("base64", Base64.encodeToString(jpeg, Base64.NO_WRAP)).put("width", bitmap.width).put("height", bitmap.height).put("screen_bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom))).put("window_bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom))).put("package", expected).put("secure_window_guard", "platform_window_capture")
        } finally { if (bitmap !== original) bitmap.recycle() }
    }
    companion object {
        @Volatile var current: BridgeAccessibility? = null
        val blockedPackages = setOf("com.android.systemui", "com.android.settings", "com.google.android.gms", "com.samsung.android.biometrics.app.setting", "com.android.permissioncontroller", "com.google.android.permissioncontroller", "com.samsung.android.permissioncontroller", "com.android.packageinstaller", "com.google.android.packageinstaller", "com.android.credentials", "com.android.credentialmanager", "com.samsung.android.settings", "com.samsung.android.knox.containeragent")
    }
}
