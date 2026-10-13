package org.nous.phonebridge

import android.app.UiAutomation
import android.content.Context
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
    private fun shell(command: String) {
        instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).use { it.readBytes() } }
    }
    private fun waitUntil(predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15000
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(200)
        }
        fail("Accessibility state did not settle")
    }
    @Before fun setup() {
        BridgeService.stop(context, true)
        shell("settings put secure enabled_accessibility_services null")
        shell("settings put secure accessibility_enabled 0")
        waitUntil { BridgeAccessibility.current == null }
        shell("wm dismiss-keyguard")
        scenario = ActivityScenario.launch(MainActivity::class.java)
        waitUntil { MainActivity.resumed }
    }
    @After fun cleanup() {
        scenario?.close()
        shell("settings put secure enabled_accessibility_services null")
        shell("settings put secure accessibility_enabled 0")
        BridgeService.stop(context, true)
    }
    @Test fun optionalAccessFreshSelectorsPasswordGuardAndSecureWindowScreenshot() {
        DurableStore(context).use {
            try { NativeHandlers(context, it).execute("screen.tree", JSONObject()); fail("Accessibility permission required") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
        }
        shell("settings put secure enabled_accessibility_services $component")
        shell("settings put secure accessibility_enabled 1")
        waitUntil { BridgeAccessibility.current != null }
        val service = BridgeAccessibility.current!!
        waitUntil { UiThread.call { service.rootInActiveWindow?.packageName?.toString() == context.packageName } }
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
        waitUntil { UiThread.call { service.nodes(service.root(), true).any { it.isPassword } } }
        val redacted = service.handle("screen.tree", Arguments(expected))
        assertFalse(redacted.toString().contains("CI password must not appear"))
        try { service.handle("screen.input", Arguments(input)); fail("Password window input must be rejected") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        val tap = JSONObject(expected.toString()).put("x", 10).put("y", 10)
        try { service.handle("screen.tap", Arguments(tap)); fail("Password window gestures must be rejected") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        scenario!!.onActivity { activity -> activity.findViewById<EditText>(R.id.device_name).apply { inputType = InputType.TYPE_CLASS_TEXT; setText("CI ordinary field") } }
        waitUntil { UiThread.call { service.nodes(service.root(), true).none { it.isPassword } } }
        try { service.handle("screen.screenshot", Arguments(expected)); fail("FLAG_SECURE screenshot or pre-34 capture must be rejected") } catch (e: BridgeFailure) { assertTrue(e.code in setOf("UNSUPPORTED", "AMBIGUOUS_STATE")) }
        val wrong = JSONObject().put("expected_package", "com.example.unexpected")
        try { service.handle("screen.tree", Arguments(wrong)); fail("Changed package must be refused") } catch (e: BridgeFailure) { assertEquals("FOREGROUND_REQUIRED", e.code) }
        try { service.handle("screen.global", Arguments(JSONObject().put("action", "unlock"))); fail("No keyguard bypass global action") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
        assertEquals(Build.VERSION.SDK_INT >= 34, Access.capabilities(context).toString().contains("screen.screenshot"))
    }
}
