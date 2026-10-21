package org.nous.phonebridge

import android.app.UiAutomation
import android.graphics.Rect
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.widget.EditText
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.RunWith
import org.junit.runners.model.Statement

@RunWith(AndroidJUnit4::class)
class AccessibilityInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private var scenario: ActivityScenario<MainActivity>? = null
    private var originalServices: String? = null
    private var settingsCaptured = false
    private val screenOps = listOf("screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global")
    @get:Rule val testName = org.junit.rules.TestName()
    @get:Rule val cleanupRule = TestRule { base, _ ->
        object : Statement() {
            override fun evaluate() {
                var failure: Throwable? = null
                try { base.evaluate() } catch (error: Throwable) { failure = error }
                val actions: List<() -> Unit> = listOf(
                    { scenario?.close(); scenario = null },
                    { BridgeService.stop(context, true) },
                    { if (settingsCaptured) restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, null) },
                    { if (settingsCaptured) waitUntil("service-cleanup") { BridgeAccessibility.current == null } },
                    { if (settingsCaptured) restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, originalServices) },
                    { ForegroundFixture.wake() }
                )
                for (action in actions) {
                    try { action() } catch (error: Throwable) {
                        if (failure == null) failure = error else if (failure !== error) failure.addSuppressed(error)
                    }
                }
                failure?.let { throw it }
            }
        }
    }
    private fun shell(command: String) = ForegroundFixture.shell(command)
    private fun waitUntil(phase: String, predicate: () -> Boolean) = ForegroundFixture.waitUntil("${testName.methodName}-$phase", 15000, predicate)
    private fun quote(value: String) = "'${value.replace("'", "'\\''")}'"
    private fun restoreSetting(key: String, value: String?) {
        shell(if (value == null) "settings delete secure $key" else "settings put secure $key ${quote(value)}")
        waitUntil("setting-$key") { Settings.Secure.getString(context.contentResolver, key) == value }
    }
    private fun refused(code: String, block: () -> Unit) {
        try { block(); fail("Expected $code refusal") } catch (error: BridgeFailure) { assertEquals(code, error.code) }
    }
    private fun assertDebugOff(handlers: NativeHandlers) {
        val status = handlers.execute("debug.status", JSONObject())
        assertTrue(status.getBoolean("experimental"))
        assertFalse(status.getBoolean("enabled"))
        assertTrue(status.has("debug_session") && status.isNull("debug_session"))
        assertEquals(0, status.getInt("remaining_seconds"))
        assertEquals(Build.VERSION.SDK_INT >= 34 && BridgeAccessibility.current != null, status.getBoolean("screenshot_supported"))
        val advertised = Access.capabilities(context)
        val capabilities = (0 until advertised.length()).map { advertised.getString(it) }.toSet()
        assertTrue("debug.status" in capabilities)
        assertTrue(capabilities.none { it.startsWith("screen.") })
    }
    private fun screenArgs(op: String): JSONObject = JSONObject().put("expected_package", context.packageName).apply {
        when (op) {
            "screen.input" -> put("resource_id", "org.nous.phonebridge:id/device_name").put("text", "Must not change")
            "screen.tap" -> put("x", 10).put("y", 10)
            "screen.swipe" -> put("x1", 10).put("y1", 100).put("x2", 10).put("y2", 10).put("duration_ms", 400)
            "screen.global" -> { remove("expected_package"); put("action", "unlock") }
        }
    }
    @Before fun setup() {
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        settingsCaptured = true
        BridgeService.stop(context, true)
        restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, null)
        waitUntil("service-disconnected") { BridgeAccessibility.current == null }
        ForegroundFixture.prepare(testName.methodName)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        waitUntil("activity-focused") { ForegroundFixture.focused() }
    }
    @Test fun optionalAccessibilityGrantRevokePreservesDebugOffAndUnknownClockGuards() {
        DurableStore(context).use { store ->
            val handlers = NativeHandlers(context, store)
            assertFalse(Access.permissions(context).getBoolean("accessibility"))
            assertDebugOff(handlers)
            for (op in screenOps) {
                refused("PERMISSION_REQUIRED") { handlers.execute(op, screenArgs(op)) }
                refused("PERMISSION_REQUIRED") { handlers.execute(op, screenArgs(op).put("debug_session", 17)) }
            }
            restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, component)
            waitUntil("service-connected") { BridgeAccessibility.current != null }
            val service = BridgeAccessibility.current!!
            assertTrue(Access.permissions(context).getBoolean("accessibility"))
            assertDebugOff(handlers)
            for (adapter in listOf(AospClockAdapter(), GoogleClockAdapter(), SamsungClockAdapter())) {
                for (op in AospClockAdapter.operations) {
                    refused("UNSUPPORTED") { adapter.execute(service, op, Arguments(JSONObject())) }
                }
            }
            val expectedClockOps = if (GoogleClockProfile.installed(context)) GoogleClockProfile.operations else emptySet()
            assertEquals(expectedClockOps, ClockAdapters(context).capabilities())
            val advertised = Access.capabilities(context)
            val capabilities = (0 until advertised.length()).map { advertised.getString(it) }.toSet()
            for (op in AospClockAdapter.operations) assertEquals(op in expectedClockOps, op in capabilities)
            waitUntil("accessible-app-root") { ForegroundFixture.focused() && UiThread.call { service.rootInActiveWindow?.packageName?.toString() == context.packageName } }
            var originalName: String? = null
            scenario!!.onActivity { activity -> originalName = activity.findViewById<EditText>(R.id.device_name).text.toString() }
            for (op in screenOps) {
                val cases = listOf(
                    screenArgs(op) to "PERMISSION_REQUIRED",
                    screenArgs(op).put("debug_session", JSONObject.NULL) to "PERMISSION_REQUIRED",
                    screenArgs(op).put("debug_session", "not-a-current-session") to "PERMISSION_REQUIRED",
                    screenArgs(op).put("debug_session", "") to "PERMISSION_REQUIRED",
                    screenArgs(op).put("debug_session", 17) to "VALIDATION_ERROR",
                    screenArgs(op).put("debug_session", true) to "VALIDATION_ERROR",
                    screenArgs(op).put("debug_session", JSONObject()) to "VALIDATION_ERROR",
                    screenArgs(op).put("debug_session", "x".repeat(129)) to "VALIDATION_ERROR",
                    screenArgs(op).put("debug_session", "invalid" + 0.toChar() + "session") to "VALIDATION_ERROR"
                )
                for ((args, code) in cases) {
                    refused(code) { handlers.execute(op, args) }
                    refused(code) { service.handle(op, Arguments(args)) }
                }
                scenario!!.onActivity { activity -> assertEquals(originalName, activity.findViewById<EditText>(R.id.device_name).text.toString()) }
                assertDebugOff(handlers)
            }
            refused("UNSUPPORTED") { UiThread.call { service.root(context.packageName) } }
            for (op in listOf("debug.enable", "debug.renew")) refused("UNSUPPORTED") { handlers.execute(op, JSONObject()) }
            assertDebugOff(handlers)
            scenario!!.onActivity { activity -> assertEquals(originalName, activity.findViewById<EditText>(R.id.device_name).text.toString()) }
            restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, null)
            waitUntil("service-revoked") { BridgeAccessibility.current == null }
            assertFalse(Access.permissions(context).getBoolean("accessibility"))
            assertEquals(expectedClockOps, ClockAdapters(context).capabilities())
            for (op in AospClockAdapter.operations) {
                refused("PERMISSION_REQUIRED") { ClockAdapters(context).execute(op, Arguments(JSONObject())) }
            }
            val revoked = Access.capabilities(context)
            val revokedCapabilities = (0 until revoked.length()).map { revoked.getString(it) }.toSet()
            for (op in AospClockAdapter.operations) assertFalse(op in revokedCapabilities)
            for (op in screenOps) {
                refused("PERMISSION_REQUIRED") { handlers.execute(op, screenArgs(op).put("debug_session", "not-a-current-session")) }
                refused("PERMISSION_REQUIRED") { handlers.execute(op, screenArgs(op).put("debug_session", 17)) }
            }
            assertDebugOff(handlers)
        }
    }
}

internal object ForegroundFixture {
    private var recoveredSystemUiModal = false
    private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
    private val context get() = instrumentation.targetContext
    private val automation get() = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).apply {
        serviceInfo = serviceInfo.apply {
            flags = flags or android.accessibilityservice.AccessibilityServiceInfo.FLAG_REPORT_VIEW_IDS or android.accessibilityservice.AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
        }
    }
    fun shell(command: String): String = automation.executeShellCommand(command).use { parcel ->
        android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { reader ->
            val buffer = CharArray(32768)
            var count = 0
            while (count < buffer.size) {
                val read = reader.read(buffer, count, buffer.size - count)
                if (read < 0) break
                count += read
            }
            String(buffer, 0, count)
        }
    }
    fun wake() {
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
    }
    fun prepare(test: String) {
        wake()
        val ui = automation
        val modal = ui.windows.singleOrNull {
            it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_SYSTEM && it.isActive && it.isFocused && it.root?.packageName?.toString() == "android"
        }
        if (modal != null) {
            capture("$test-preexisting-system-modal")
            val emulator = shell("getprop ro.kernel.qemu").trim() == "1" || shell("getprop ro.boot.qemu").trim() == "1"
            if (!emulator) fail("Preexisting framework modal on a physical device; inspect foreground evidence")
            if (recoveredSystemUiModal) fail("Framework modal recurred after initial emulator recovery; inspect foreground evidence")
            val root = modal.root ?: error("Observed framework modal has no readable root")
            val titles = root.findAccessibilityNodeInfosByViewId("android:id/alertTitle").filter { it.isVisibleToUser && !it.isPassword }.map { it.text?.toString() ?: "" }
            val waits = root.findAccessibilityNodeInfosByViewId("android:id/aerr_wait").filter { it.isVisibleToUser && it.isEnabled && it.isClickable && !it.isPassword && it.text?.toString() == "Wait" }
            if (titles.none { it.contains("System UI") && it.contains("isn't responding") } || waits.size != 1) fail("Unidentified preexisting framework modal; inspect foreground evidence before any dismissal")
            recoveredSystemUiModal = true
            assertTrue("Platform ANR Wait action refused", waits.single().performAction(android.view.accessibility.AccessibilityNodeInfo.ACTION_CLICK))
            waitUntil("$test-system-modal-closed", 2500) { automation.windows.none { it.id == modal.id } }
            capture("$test-system-modal-closed")
        }
        waitUntil("$test-awake-unlocked", 2500) {
            context.getSystemService(android.os.PowerManager::class.java).isInteractive && !context.getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked
        }
    }
    fun focused(): Boolean = UiThread.call {
        MainActivity.resumed && !context.getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked && context.getSystemService(android.os.PowerManager::class.java).isInteractive &&
            androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry.getInstance().getActivitiesInStage(androidx.test.runner.lifecycle.Stage.RESUMED).any {
                it is MainActivity && !it.isFinishing && !it.isDestroyed && it.hasWindowFocus() && it.window.decorView.isAttachedToWindow && it.window.decorView.isShown
            }
    }
    fun waitUntil(phase: String, timeoutMs: Long, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            try { if (predicate()) return } catch (e: Exception) {
                capture(phase)
                throw e
            }
            Thread.sleep(10)
        }
        val report = capture(phase)
        fail("Foreground fixture did not settle: $phase; evidence=$report")
    }
    fun capture(phase: String): String {
        val report = JSONObject().put("phase", phase).put("sdk", Build.VERSION.SDK_INT).put("monotonic_ms", SystemClock.elapsedRealtime()).put("resumed", MainActivity.resumed)
        report.put("keyguard_locked", context.getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked)
        report.put("device_locked", context.getSystemService(android.app.KeyguardManager::class.java).isDeviceLocked)
        report.put("interactive", context.getSystemService(android.os.PowerManager::class.java).isInteractive)
        val activities = java.util.concurrent.FutureTask(java.util.concurrent.Callable {
            val entries = org.json.JSONArray()
            val monitor = androidx.test.runner.lifecycle.ActivityLifecycleMonitorRegistry.getInstance()
            for (stage in androidx.test.runner.lifecycle.Stage.values()) monitor.getActivitiesInStage(stage).take(8).forEach { activity ->
                entries.put(JSONObject().put("activity", activity.javaClass.name).put("identity", System.identityHashCode(activity)).put("stage", stage.name).put("focus", activity.hasWindowFocus()).put("finishing", activity.isFinishing).put("destroyed", activity.isDestroyed).put("window_flags", activity.window.attributes.flags).put("attached", activity.window.decorView.isAttachedToWindow).put("shown", activity.window.decorView.isShown))
            }
            entries
        })
        android.os.Handler(android.os.Looper.getMainLooper()).post(activities)
        try { report.put("activities", activities.get(1000, java.util.concurrent.TimeUnit.MILLISECONDS)) } catch (e: Exception) {
            activities.cancel(false)
            report.put("activity_snapshot_error", e.javaClass.simpleName)
        }
        val ui = automation
        try {
            report.put("automation_root_package", ui.rootInActiveWindow?.packageName?.toString() ?: JSONObject.NULL)
            report.put("service_root_package", BridgeAccessibility.current?.rootInActiveWindow?.packageName?.toString() ?: JSONObject.NULL)
            val windows = org.json.JSONArray()
            for (window in ui.windows.take(8)) {
                val root = window.root
                val bounds = Rect().also { window.getBoundsInScreen(it) }
                val entry = JSONObject().put("id", window.id).put("type", window.type).put("active", window.isActive).put("focused", window.isFocused).put("package", root?.packageName?.toString() ?: JSONObject.NULL).put("bounds", org.json.JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                if (root?.packageName?.toString() == "android") {
                    val nodes = org.json.JSONArray()
                    fun visit(node: android.view.accessibility.AccessibilityNodeInfo, depth: Int) {
                        if (depth > 16 || nodes.length() >= 80 || node.isPassword) return
                        nodes.put(JSONObject().put("id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString()).put("text", node.text?.toString()?.take(256)).put("visible", node.isVisibleToUser).put("enabled", node.isEnabled))
                        for (index in 0 until node.childCount.coerceAtMost(40)) node.getChild(index)?.let { visit(it, depth + 1) }
                    }
                    visit(root, 0)
                    entry.put("framework_nodes", nodes)
                }
                windows.put(entry)
            }
            report.put("windows", windows)
        } catch (e: Exception) { report.put("window_snapshot_error", e.javaClass.simpleName) }
        report.put("window_manager", shell("dumpsys window windows").take(12000))
        val directory = java.io.File(context.filesDir, "foreground-ci").apply { mkdirs() }
        val name = "api${Build.VERSION.SDK_INT}-${phase.replace(Regex("[^A-Za-z0-9_-]"), "_").take(140)}"
        try {
            val image = ui.takeScreenshot()
            if (image == null) report.put("screenshot", "platform-refused") else {
                try {
                    val ratio = minOf(1.0, 480.0 / maxOf(image.width, image.height))
                    val scaled = android.graphics.Bitmap.createScaledBitmap(image, (image.width * ratio).toInt().coerceAtLeast(1), (image.height * ratio).toInt().coerceAtLeast(1), true)
                    try {
                        val file = java.io.File(directory, "$name.jpg")
                        file.outputStream().use { scaled.compress(android.graphics.Bitmap.CompressFormat.JPEG, 50, it) }
                        report.put("screenshot", file.absolutePath).put("secure_windows", "platform-redacted")
                    } finally { if (scaled !== image) scaled.recycle() }
                } finally { image.recycle() }
            }
        } catch (e: Exception) { report.put("screenshot_error", e.javaClass.simpleName) }
        val file = java.io.File(directory, "$name.json")
        file.writeText(report.toString())
        val chunks = report.toString().chunked(1500)
        chunks.forEachIndexed { index, text -> android.util.Log.i("ForegroundEvidence", JSONObject().put("phase", phase).put("part", index).put("parts", chunks.size).put("json", text).toString()) }
        instrumentation.sendStatus(0, android.os.Bundle().apply { putString("foregroundEvidence", file.absolutePath) })
        return file.absolutePath
    }
}
