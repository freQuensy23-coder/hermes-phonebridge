package org.nous.phonebridge

import android.app.UiAutomation
import android.graphics.Rect
import android.os.Build
import android.os.SystemClock
import android.text.InputType
import android.widget.EditText
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class AccessibilityInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private var scenario: ActivityScenario<MainActivity>? = null
    @get:org.junit.Rule val testName = org.junit.rules.TestName()
    private fun shell(command: String) = ForegroundFixture.shell(command)
    private fun waitUntil(phase: String, predicate: () -> Boolean) = ForegroundFixture.waitUntil("${testName.methodName}-$phase", 15000, predicate)
    @Before fun setup() {
        BridgeService.stop(context, true)
        shell("settings put secure enabled_accessibility_services null")
        shell("settings put secure accessibility_enabled 0")
        waitUntil("service-disconnected") { BridgeAccessibility.current == null }
        ForegroundFixture.prepare(testName.methodName)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        waitUntil("activity-focused") { ForegroundFixture.focused() }
    }
    @After fun cleanup() {
        try {
            scenario?.close()
        } finally {
            scenario = null
            try {
                shell("settings put secure enabled_accessibility_services null")
                shell("settings put secure accessibility_enabled 0")
                waitUntil("service-cleanup") { BridgeAccessibility.current == null }
            } finally {
                BridgeService.stop(context, true)
                ForegroundFixture.wake()
            }
        }
    }
    @Test fun optionalAccessFreshSelectorsPasswordGuardAndSecureWindowScreenshot() {
        DurableStore(context).use {
            try { NativeHandlers(context, it).execute("screen.tree", JSONObject()); fail("Accessibility permission required") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
        }
        shell("settings put secure enabled_accessibility_services $component")
        shell("settings put secure accessibility_enabled 1")
        waitUntil("service-connected") { BridgeAccessibility.current != null }
        val service = BridgeAccessibility.current!!
        for (adapter in listOf(AospClockAdapter(), GoogleClockAdapter(), SamsungClockAdapter())) {
            for (op in AospClockAdapter.operations) {
                try { adapter.execute(service, op, Arguments(JSONObject())); fail("Unidentified clock profile must refuse every UI operation") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
            }
        }
        val expectedClockOps = if (GoogleClockProfile.installed(context)) GoogleClockProfile.operations else emptySet()
        assertEquals(expectedClockOps, ClockAdapters(context).capabilities())
        for (op in AospClockAdapter.operations) assertEquals(op in expectedClockOps, Access.capabilities(context).toString().contains(op))
        waitUntil("accessible-app-root") { ForegroundFixture.focused() && UiThread.call { service.rootInActiveWindow?.packageName?.toString() == context.packageName } }
        val expected = JSONObject().put("expected_package", context.packageName)
        val initial = service.handle("screen.tree", Arguments(expected))
        assertEquals(context.packageName, initial.getString("package"))
        assertTrue(initial.getBoolean("password_nodes_excluded"))
        assertTrue(initial.getJSONArray("nodes").length() > 0)
        val resource = "org.nous.phonebridge:id/device_name"
        val input = expected.toString().let { JSONObject(it) }.put("resource_id", resource).put("text", "CI readback")
        val result = service.handle("screen.input", Arguments(input))
        assertTrue(result.getBoolean("verified"))
        scenario!!.onActivity { activity -> assertEquals("CI readback", activity.findViewById<EditText>(R.id.device_name).text.toString()) }
        scenario!!.onActivity { activity ->
            val field = activity.findViewById<EditText>(R.id.device_name)
            field.inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
            field.setText("CI password must not appear")
        }
        waitUntil("password-visible") { UiThread.call { service.nodes(service.root(), true).any { it.isPassword } } }
        val redacted = service.handle("screen.tree", Arguments(expected))
        assertFalse(redacted.toString().contains("CI password must not appear"))
        try { service.handle("screen.input", Arguments(input)); fail("Password window input must be rejected") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        val tap = JSONObject(expected.toString()).put("x", 10).put("y", 10)
        try { service.handle("screen.tap", Arguments(tap)); fail("Password window gestures must be rejected") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        scenario!!.onActivity { activity -> activity.findViewById<EditText>(R.id.device_name).apply { inputType = InputType.TYPE_CLASS_TEXT; setText("CI ordinary field") } }
        waitUntil("password-cleared") { UiThread.call { service.nodes(service.root(), true).none { it.isPassword } } }
        try { service.handle("screen.screenshot", Arguments(expected)); fail("FLAG_SECURE screenshot or pre-34 capture must be rejected") } catch (e: BridgeFailure) { assertTrue(e.code in setOf("UNSUPPORTED", "AMBIGUOUS_STATE")) }
        val wrong = JSONObject().put("expected_package", "com.example.unexpected")
        try { service.handle("screen.tree", Arguments(wrong)); fail("Changed package must be refused") } catch (e: BridgeFailure) { assertEquals("FOREGROUND_REQUIRED", e.code) }
        try { service.handle("screen.global", Arguments(JSONObject().put("action", "unlock"))); fail("No keyguard bypass global action") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
        assertEquals(Build.VERSION.SDK_INT >= 34, Access.capabilities(context).toString().contains("screen.screenshot"))
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
