package org.nous.phonebridge

import android.accessibilityservice.AccessibilityServiceInfo
import android.app.UiAutomation
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Rect
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.provider.Settings
import android.util.Base64
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.ConnectionPool
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.RunWith
import org.junit.runners.model.Statement
import java.io.File
import java.util.Calendar
import java.util.Locale
import java.util.UUID
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class DebugControlInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val automation get() = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
    private val fixture = "org.nous.phonebridge.debugfixture"
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private val adminHttp = BridgeHttp.client.newBuilder().connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS)).retryOnConnectionFailure(false).callTimeout(15, TimeUnit.SECONDS).build()
    private lateinit var server: String
    private lateinit var publicServer: String
    private lateinit var adminToken: String
    private var deviceId: String? = null
    private var scenario: ActivityScenario<MainActivity>? = null
    private var originalServices: String? = null
    private var originalEnabled: String? = null
    private var settingsCaptured = false
    private var originalAutomationFlags: Int? = null
    private val clockPackage = "com.google.android.deskclock"
    private var clockFixtureActive = false
    private var originalTimeFormat: String? = null
    private val wholeFlowDirectory get() = File(context.filesDir, "debug-wholeflow")
    private val wholeFlowPhases = mutableListOf<String>()

    @get:Rule val cleanupRule = TestRule { base, _ ->
        object : Statement() {
            override fun evaluate() {
                var failure: Throwable? = null
                try { base.evaluate() } catch (error: Throwable) { failure = error }
                val actions: List<() -> Unit> = listOf(
                    { deviceId?.let { http("DELETE", "/v1/devices/$it", expected = 204) }; Unit },
                    { deviceId?.let { assertEquals("NOT_FOUND", http("GET", "/v1/devices/$it", expected = 404).getJSONObject("error").getString("code")) }; Unit },
                    {
                        deviceId?.let { id ->
                            val devices = http("GET", "/v1/devices").getJSONArray("devices")
                            assertFalse((0 until devices.length()).any { devices.getJSONObject(it).getString("id") == id })
                        }
                    },
                    { scenario?.close(); scenario = null },
                    { BridgeService.stop(context, true) },
                    { waitUntil { BridgeService.current == null && Credentials(context).load() == null } },
                    { if (settingsCaptured) restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, originalServices) },
                    { if (settingsCaptured) restoreSetting(Settings.Secure.ACCESSIBILITY_ENABLED, originalEnabled) },
                    { shell("am force-stop $fixture"); Unit },
                    {
                        if (clockFixtureActive) {
                            shell("am force-stop $clockPackage")
                            assertTrue(shell("pm clear $clockPackage").contains("Success"))
                            shell(if (originalTimeFormat == null) "settings delete system time_12_24" else "settings put system time_12_24 ${quote(originalTimeFormat!!)}")
                            waitUntil { Settings.System.getString(context.contentResolver, Settings.System.TIME_12_24) == originalTimeFormat }
                        }
                    },
                    {
                        originalAutomationFlags?.let { flags ->
                            val serviceInfo = automation.serviceInfo ?: error("Independent automation service info unavailable during restore")
                            serviceInfo.flags = flags
                            automation.serviceInfo = serviceInfo
                            assertEquals(flags, automation.serviceInfo.flags)
                        }
                    },
                    { adminHttp.connectionPool.evictAll() }
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

    private fun shell(command: String): String {
        val descriptors = automation.executeShellCommandRw("sh")
        return ParcelFileDescriptor.AutoCloseInputStream(descriptors[0]).use { output ->
            ParcelFileDescriptor.AutoCloseOutputStream(descriptors[1]).bufferedWriter().use { input ->
                input.write(command)
                input.newLine()
            }
            output.bufferedReader().use { it.readText() }
        }
    }
    private fun quote(value: String) = "'${value.replace("'", "'\\''")}'"
    private fun restoreSetting(key: String, value: String?) {
        shell(if (value == null) "settings delete secure $key" else "settings put secure $key ${quote(value)}")
        waitUntil { Settings.Secure.getString(context.contentResolver, key) == value }
    }
    private fun waitUntil(timeoutMs: Long = 30000, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(150)
        }
        fail("Required debug CI condition did not become true before deadline")
    }
    private data class HttpReply(val status: Int, val body: JSONObject)
    private fun request(method: String, path: String, body: JSONObject? = null): HttpReply {
        val request = Request.Builder().url(server + path).header("Authorization", "Bearer $adminToken").header("Connection", "close")
            .method(method, body?.toString()?.toRequestBody("application/json".toMediaType())).build()
        return adminHttp.newCall(request).execute().use { response ->
            HttpReply(response.code, response.body?.string().orEmpty().let { if (it.isEmpty()) JSONObject() else JSONObject(it) })
        }
    }
    private fun http(method: String, path: String, body: JSONObject? = null, expected: Int = 200): JSONObject {
        val response = request(method, path, body)
        assertEquals("Unexpected synthetic server HTTP status", expected, response.status)
        return response.body
    }
    private fun commandPath() = "/v1/devices/${deviceId ?: error("Missing paired device")}/commands"
    private fun commandBody(op: String, args: JSONObject) = JSONObject().put("op", op).put("args", args).put("ttl_seconds", 60)
    private fun command(op: String, args: JSONObject): JSONObject {
        val queued = http("POST", commandPath(), commandBody(op, args))
        return completedCommand(queued)
    }
    private fun completedCommand(queued: JSONObject): JSONObject {
        var result = JSONObject()
        waitUntil {
            result = http("GET", "/v1/commands/${queued.getString("id")}")
            result.getString("status") in setOf("succeeded", "failed", "expired")
        }
        assertEquals(queued.getString("id"), result.getString("id"))
        return result
    }
    private fun advertisedScreenOperations(): Set<String> {
        val device = http("GET", "/v1/devices/${deviceId ?: error("Missing paired device")}")
        assertTrue("Capability evidence requires the connected real device", device.getBoolean("online"))
        val capabilities = device.getJSONArray("capabilities")
        return (0 until capabilities.length()).map { capabilities.getString(it) }.filter { it.startsWith("screen.") }.toSet()
    }
    private fun waitForScreenCapabilities(enabled: Boolean) {
        val expected = if (enabled) setOf("screen.tree", "screen.tap", "screen.swipe", "screen.input", "screen.global") +
            (if (Build.VERSION.SDK_INT >= 34) setOf("screen.screenshot") else emptySet()) else emptySet()
        waitUntil { advertisedScreenOperations() == expected }
    }
    private fun serverEnqueueUnsupported(op: String, args: JSONObject) {
        assertFalse("This assertion covers an unadvertised operation at server enqueue, not Android refusal", op in advertisedScreenOperations())
        val response = http("POST", commandPath(), commandBody(op, args), expected = 422)
        assertEquals("UNSUPPORTED", response.getJSONObject("error").getString("code"))
        assertFalse("Rejected enqueue must not invent a correlated command", response.has("id"))
        assertFalse(response.has("data") && !response.isNull("data"))
    }
    private fun unavailableConsentRefusal(op: String, args: JSONObject) {
        val staleHelloAdvertised = op in advertisedScreenOperations()
        val response = request("POST", commandPath(), commandBody(op, args))
        if (response.status == 422) {
            assertEquals("Server enqueue layer must specifically reject an unavailable capability", "UNSUPPORTED", response.body.getJSONObject("error").getString("code"))
            assertFalse(response.body.has("id"))
            assertFalse(response.body.has("data") && !response.body.isNull("data"))
        } else {
            assertEquals("Only HTTP 200 queued or HTTP 422 UNSUPPORTED is permitted", 200, response.status)
            assertTrue("Queued off-state command requires a previously advertised stale hello", staleHelloAdvertised)
            refused(completedCommand(response.body), "PERMISSION_REQUIRED")
        }
    }
    private fun captureRefused(session: String, pkg: String = fixture) {
        if (Build.VERSION.SDK_INT == 31) serverEnqueueUnsupported("screen.screenshot", args(session, pkg))
        else refused(command("screen.screenshot", args(session, pkg)), "UNSUPPORTED")
    }
    private fun success(value: JSONObject): JSONObject {
        val observedCode = value.optJSONObject("error")?.optString("code").orEmpty()
        val allowedCodes = setOf("UNSUPPORTED", "PERMISSION_REQUIRED", "FOREGROUND_REQUIRED", "PHONE_LOCKED", "AMBIGUOUS_STATE", "VALIDATION_ERROR", "NOT_FOUND", "EXPIRED", "INTERNAL_ERROR")
        val code = observedCode.takeIf { it in allowedCodes } ?: "UNCLASSIFIED"
        val reason = when (value.optJSONObject("error")?.optString("message")) {
            "System, input method and overlay windows are excluded" -> "SYSTEM_WINDOW"
            "Cannot inspect relevant window", "Cannot inspect selected window" -> "MISSING_WINDOW_ROOT"
            "Password-bearing windows are excluded" -> "PASSWORD_WINDOW"
            "Cannot inspect advertised window child", "Window exceeds complete password-guard traversal bound" -> "INCOMPLETE_TREE"
            "Relevant window has a different owner" -> "FOREIGN_OWNER"
            "Another window overlaps the selected native window" -> "OVERLAP"
            "PhoneBridge consent/settings UI cannot be remotely controlled", "PhoneBridge, permission, credential and lock screens are excluded" -> "OWN_UI"
            "Protected UI window is present" -> "PROTECTED_UI"
            "Selected window must be an active focused application with native bounds" -> "INVALID_ACTIVE_WINDOW"
            "No inspectable active window graph", "Selected active window is missing" -> "INVALID_WINDOW_GRAPH"
            else -> "UNCLASSIFIED"
        }
        assertEquals("Command must succeed through the production HTTP and WSS path; bridge_code=$code bridge_reason=$reason", "succeeded", value.getString("status"))
        return value.getJSONObject("data")
    }
    private fun refused(value: JSONObject, code: String) {
        assertEquals("failed", value.getString("status"))
        assertEquals(code, value.getJSONObject("error").getString("code"))
        assertFalse(value.has("data") && !value.isNull("data"))
    }
    private fun debugStatus() = success(command("debug.status", JSONObject()))
    private fun args(session: String, pkg: String = fixture) = JSONObject().put("debug_session", session).put("expected_package", pkg)
    private fun tree(session: String) = success(command("screen.tree", args(session)))
    private fun treeNode(tree: JSONObject, name: String): JSONObject {
        val nodes = tree.getJSONArray("nodes")
        val found = (0 until nodes.length()).map { nodes.getJSONObject(it) }.filter { it.optString("resource_id") == "$fixture:id/$name" && it.optBoolean("visible") }
        assertEquals("A real visible fixture selector must be unique", 1, found.size)
        return found.single()
    }
    private fun center(node: JSONObject): Pair<Int, Int> {
        val bounds = node.getJSONArray("bounds")
        assertEquals(4, bounds.length())
        assertTrue(bounds.getInt(2) > bounds.getInt(0))
        assertTrue(bounds.getInt(3) > bounds.getInt(1))
        return Pair((bounds.getInt(0) + bounds.getInt(2)) / 2, (bounds.getInt(1) + bounds.getInt(3)) / 2)
    }
    private fun localNodes(): List<AccessibilityNodeInfo> {
        val nodes = mutableListOf<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 32 || nodes.size >= 400) return
            nodes.add(node)
            repeat(node.childCount.coerceAtMost(100)) { node.getChild(it)?.let { child -> visit(child, depth + 1) } }
        }
        automation.rootInActiveWindow?.let { visit(it, 0) }
        return nodes
    }
    private fun localText(name: String): String? = localNodes().singleOrNull { it.viewIdResourceName == "$fixture:id/$name" }?.text?.toString()
    private fun localControl(id: String): AccessibilityNodeInfo {
        for (attempt in 0 until 24) {
            val scroll = localNodes().firstOrNull { it.isScrollable } ?: break
            if (!scroll.performAction(AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD)) break
            Thread.sleep(80)
        }
        repeat(24) {
            val nodes = localNodes()
            val node = nodes.singleOrNull { it.viewIdResourceName == id && it.isVisibleToUser }
            if (node != null) {
                val bounds = Rect().also { node.getBoundsInScreen(it) }
                if (!bounds.isEmpty) return node
            }
            nodes.firstOrNull { it.isScrollable }?.performAction(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD)
            Thread.sleep(150)
        }
        error("Required local consent control was not visible")
    }
    private fun localClick(id: String) {
        val bounds = Rect().also { localControl(id).getBoundsInScreen(it) }
        shell("input tap ${bounds.centerX()} ${bounds.centerY()}")
        instrumentation.waitForIdleSync()
    }
    private fun showMain() {
        shell("am start -W -f 0x00020000 -n org.nous.phonebridge/.MainActivity")
        waitUntil { automation.rootInActiveWindow?.packageName?.toString() == context.packageName }
    }
    private fun showFixture(mode: String = "ordinary") {
        shell("am force-stop $fixture")
        shell("am start -W -n $fixture/.FixtureActivity --es screen $mode")
        waitUntil { localText("counter") == "Counter: 0" && localText("editor") == "Synthetic initial text" }
        Thread.sleep(400)
    }
    private fun localEnable(button: String = "debug_enable_manual"): String {
        showMain()
        localClick("org.nous.phonebridge:id/$button")
        waitUntil { localNodes().any { it.viewIdResourceName == "android:id/button1" } }
        assertFalse(debugStatus().getBoolean("enabled"))
        localClick("android:id/button1")
        val status = debugStatus()
        assertTrue(status.getBoolean("experimental"))
        assertTrue(status.getBoolean("enabled"))
        assertFalse(status.isNull("debug_session"))
        waitForScreenCapabilities(true)
        return status.getString("debug_session")
    }
    private fun localStop() {
        showMain()
        localClick("org.nous.phonebridge:id/debug_stop")
        val status = debugStatus()
        assertFalse(status.getBoolean("enabled"))
        assertTrue(status.isNull("debug_session"))
        assertEquals(0, status.getInt("remaining_seconds"))
    }
    private fun dispatched(result: JSONObject) {
        assertEquals("dispatched", result.getString("outcome"))
        assertFalse(result.getBoolean("verified"))
    }

    @Before fun setup() {
        val supplied = InstrumentationRegistry.getArguments()
        assertEquals("true", supplied.getString("automaticDebugCi"))
        assertEquals("1", supplied.getString("ciRunAttempt"))
        assertTrue(Build.HARDWARE in setOf("ranchu", "goldfish"))
        assertTrue(Build.VERSION.SDK_INT in setOf(31, 35))
        server = supplied.getString("serverUrl") ?: error("Automatic CI must supply serverUrl")
        publicServer = supplied.getString("expectedPublicServerUrl") ?: error("Automatic CI must supply expectedPublicServerUrl")
        adminToken = supplied.getString("adminToken") ?: error("Automatic CI must supply synthetic token")
        assertEquals("http://10.0.2.2:18940", server)
        assertTrue(publicServer.matches(Regex("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.trycloudflare\\.com")))
        assertTrue(adminToken.matches(Regex("[A-Za-z0-9_-]{64}")))
        val serviceInfo = automation.serviceInfo ?: error("Independent automation service info unavailable")
        originalAutomationFlags = serviceInfo.flags
        serviceInfo.flags = serviceInfo.flags or AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
        automation.serviceInfo = serviceInfo
        assertTrue(automation.serviceInfo.flags and AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS != 0)
        BridgeService.stop(context, true)
        waitUntil { BridgeService.current == null }
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        originalEnabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED)
        settingsCaptured = true
        val services = originalServices.orEmpty().split(':').filter { it.isNotEmpty() }.plus(component).distinct().joinToString(":")
        shell("settings put secure enabled_accessibility_services ${quote(services)}")
        shell("settings put secure accessibility_enabled 1")
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        waitUntil { BridgeAccessibility.current != null }
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Debug synthetic CI").put("ttl_seconds", 300))
        val payload = pairing.getJSONObject("qr_payload")
        assertEquals(publicServer, payload.getString("server"))
        val qr = PairingQr.parse(payload.toString(), false)
        val claimed = BridgeHttp.claim(qr, "Debug automatic CI", false)
        deviceId = claimed.deviceId
        assertEquals("$publicServer/", claimed.server)
        assertEquals("wss://${qr.server.host}/v1/device/ws", claimed.wsUrl)
        Credentials(context).save(claimed)
        assertEquals(claimed, Credentials(context).load())
        val plainPrefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(plainPrefs.contains(claimed.token))
        assertFalse(plainPrefs.contains(qr.token))
        scenario = ActivityScenario.launch(MainActivity::class.java)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { http("GET", "/v1/devices/${claimed.deviceId}").optBoolean("online") }
        val off = debugStatus()
        assertTrue(off.getBoolean("experimental"))
        assertFalse(off.getBoolean("enabled"))
        assertTrue(off.isNull("debug_session"))
        assertEquals(0, off.getInt("remaining_seconds"))
        waitForScreenCapabilities(false)
    }

    @Test fun realHttpWsTreeCoordinateTapInputAndSingleSwipeWithIndependentReadback() {
        val session = localEnable()
        showFixture()
        val initial = tree(session)
        assertEquals(fixture, initial.getString("package"))
        assertEquals("Counter: 0", treeNode(initial, "counter").getString("text"))
        refused(command("screen.tree", args(session, "org.nous.phonebridge.synthetic.mismatch")), "FOREGROUND_REQUIRED")
        assertEquals("Counter: 0", localText("counter"))
        assertEquals("Synthetic initial text", localText("editor"))
        assertEquals(fixture, automation.rootInActiveWindow?.packageName?.toString())
        refused(command("screen.global", JSONObject().put("debug_session", session).put("action", "unlock")), "VALIDATION_ERROR")
        assertEquals("Counter: 0", localText("counter"))
        assertEquals("Synthetic initial text", localText("editor"))
        assertEquals(fixture, automation.rootInActiveWindow?.packageName?.toString())
        val point = center(treeNode(initial, "increment"))
        val tap = success(command("screen.tap", args(session).put("x", point.first).put("y", point.second)))
        dispatched(tap)
        assertTrue(tap.getBoolean("gesture_completed"))
        waitUntil { localText("counter") == "Counter: 1" }
        assertEquals("Counter: 1", treeNode(tree(session), "counter").getString("text"))
        Thread.sleep(300)
        assertEquals("Counter: 1", localText("counter"))
        val requested = "Synthetic HTTP WSS input"
        success(command("screen.input", args(session).put("resource_id", "$fixture:id/editor").put("expected_text", "Synthetic initial text").put("text", requested)))
        waitUntil { localText("editor") == requested }
        assertEquals(requested, treeNode(tree(session), "editor").getString("text"))
        val scroll = treeNode(tree(session), "scroll").getJSONArray("bounds")
        val x = (scroll.getInt(0) + scroll.getInt(2)) / 2
        val top = scroll.getInt(1)
        val bottom = scroll.getInt(3)
        val y1 = bottom - (bottom - top) / 5
        val y2 = top + (bottom - top) / 5
        val before = localNodes().filter { it.isVisibleToUser && it.text?.startsWith("Synthetic scroll row") == true }.map { it.text.toString() }
        val swipe = success(command("screen.swipe", args(session).put("x1", x).put("y1", y1).put("x2", x).put("y2", y2).put("duration_ms", 400)))
        dispatched(swipe)
        assertTrue(swipe.getBoolean("gesture_completed"))
        waitUntil { localNodes().filter { it.isVisibleToUser && it.text?.startsWith("Synthetic scroll row") == true }.map { it.text.toString() } != before }
        assertEquals("Counter: 1", localText("counter"))
    }

    @Test fun realPlatformScreenshotApi35OrExplicitApi31Unsupported() {
        val session = localEnable()
        showFixture()
        val status = debugStatus()
        assertEquals(Build.VERSION.SDK_INT >= 34, status.getBoolean("screenshot_supported"))
        if (Build.VERSION.SDK_INT == 31) {
            serverEnqueueUnsupported("screen.screenshot", args(session))
            assertEquals(fixture, tree(session).getString("package"))
            return
        }
        val image = success(command("screen.screenshot", args(session)))
        assertEquals("image/jpeg", image.getString("mime"))
        assertEquals(fixture, image.getString("package"))
        val bounds = image.getJSONArray("screen_bounds")
        val windowBounds = image.getJSONArray("window_bounds")
        assertEquals("platform_window_capture", image.getString("secure_window_guard"))
        assertEquals(4, bounds.length())
        assertEquals(4, windowBounds.length())
        val localWindow = automation.rootInActiveWindow?.window ?: error("Actual fixture window must be independently observable")
        val actualWindowBounds = Rect().also { localWindow.getBoundsInScreen(it) }
        assertEquals(fixture, localWindow.root?.packageName?.toString())
        val nativeBounds = listOf(actualWindowBounds.left, actualWindowBounds.top, actualWindowBounds.right, actualWindowBounds.bottom)
        for (index in nativeBounds.indices) {
            assertEquals("Capture coordinates must be native fixture-window coordinates, not full-display context", nativeBounds[index], bounds.getInt(index))
            assertEquals(bounds.getInt(index), windowBounds.getInt(index))
        }
        val nativeWidth = bounds.getInt(2) - bounds.getInt(0)
        val nativeHeight = bounds.getInt(3) - bounds.getInt(1)
        assertTrue(nativeWidth > 0 && nativeHeight > 0)
        val buttonBounds = treeNode(tree(session), "increment").getJSONArray("bounds")
        assertTrue(buttonBounds.getInt(0) >= bounds.getInt(0) && buttonBounds.getInt(1) >= bounds.getInt(1))
        assertTrue(buttonBounds.getInt(2) <= bounds.getInt(2) && buttonBounds.getInt(3) <= bounds.getInt(3))
        val bytes = Base64.decode(image.getString("base64"), Base64.NO_WRAP)
        assertTrue(bytes.size in 1024..245760)
        assertEquals(0xff, bytes[0].toInt() and 255)
        assertEquals(0xd8, bytes[1].toInt() and 255)
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: error("Real JPEG must decode")
        try {
            assertEquals(image.getInt("width"), bitmap.width)
            assertEquals(image.getInt("height"), bitmap.height)
            assertTrue(bitmap.width > 32 && bitmap.height > 32)
            assertTrue(maxOf(bitmap.width, bitmap.height) in 241..1280)
            assertTrue(kotlin.math.abs(bitmap.width.toDouble() / bitmap.height - nativeWidth.toDouble() / nativeHeight) < 0.03)
            val pixels = mutableSetOf<Int>()
            for (y in 0 until bitmap.height step maxOf(1, bitmap.height / 24)) {
                for (x in 0 until bitmap.width step maxOf(1, bitmap.width / 24)) pixels.add(bitmap.getPixel(x, y))
            }
            assertTrue("Screenshot must contain nontrivial actual fixture pixels", pixels.size > 12)
        } finally { bitmap.recycle() }
    }

    @Test fun localWarningCancellationAndAllFourDurationsRequireConfirmation() {
        showMain()
        localClick("org.nous.phonebridge:id/debug_enable_10")
        waitUntil { localNodes().any { it.viewIdResourceName == "android:id/button2" } }
        assertFalse(debugStatus().getBoolean("enabled"))
        localClick("android:id/button2")
        assertFalse(debugStatus().getBoolean("enabled"))
        val options = listOf("debug_enable_10" to 600, "debug_enable_15" to 900, "debug_enable_60" to 3600, "debug_enable_manual" to null)
        for ((button, seconds) in options) {
            localEnable(button)
            val status = debugStatus()
            if (seconds == null) assertTrue(status.isNull("remaining_seconds")) else assertTrue(status.getInt("remaining_seconds") in (seconds - 30)..seconds)
            assertTrue(localNodes().any { it.viewIdResourceName == "org.nous.phonebridge:id/debug_status" })
            localStop()
        }
    }

    @Test fun localStopAndFreshEnableRejectOldGeneration() {
        val old = localEnable()
        showFixture()
        val point = center(treeNode(tree(old), "increment"))
        localStop()
        showFixture()
        unavailableConsentRefusal("screen.tap", args(old).put("x", point.first).put("y", point.second))
        assertEquals("Counter: 0", localText("counter"))
        val fresh = localEnable()
        assertNotEquals(old, fresh)
        showFixture()
        waitForScreenCapabilities(true)
        assertEquals(fixture, tree(fresh).getString("package"))
        assertTrue(setOf("screen.tree", "screen.tap").all { it in advertisedScreenOperations() })
        refused(command("screen.tree", args(old)), "PERMISSION_REQUIRED")
        refused(command("screen.tap", args(old).put("x", point.first).put("y", point.second)), "PERMISSION_REQUIRED")
        assertEquals("Counter: 0", localText("counter"))
        assertEquals(fixture, tree(fresh).getString("package"))
    }

    @Test fun ownConsentControlsCannotBeRemotelyReadOrOperated() {
        val session = localEnable()
        showMain()
        val bounds = Rect().also { localControl("org.nous.phonebridge:id/debug_stop").getBoundsInScreen(it) }
        for (op in listOf("screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global")) {
            val value = args(session, context.packageName)
            when (op) {
                "screen.tap" -> value.put("x", bounds.centerX()).put("y", bounds.centerY())
                "screen.swipe" -> value.put("x1", bounds.centerX()).put("y1", bounds.centerY()).put("x2", bounds.centerX()).put("y2", bounds.centerY() - 20).put("duration_ms", 400)
                "screen.input" -> value.put("resource_id", "org.nous.phonebridge:id/device_name").put("text", "Synthetic refusal")
                "screen.global" -> { value.remove("expected_package"); value.put("action", "back") }
            }
            if (op == "screen.screenshot") captureRefused(session, context.packageName)
            else refused(command(op, value), "UNSUPPORTED")
            assertTrue(debugStatus().getBoolean("enabled"))
            assertEquals(context.packageName, automation.rootInActiveWindow?.packageName?.toString())
        }
    }

    @Test fun accessibilitySettingLossRevokesConsentAndPreventsEffects() {
        val session = localEnable()
        showFixture()
        val point = center(treeNode(tree(session), "increment"))
        val enabledServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        val withoutBridge = enabledServices.orEmpty().split(':').filter { it.isNotEmpty() && it != component && it != "org.nous.phonebridge/.BridgeAccessibility" }.joinToString(":")
        restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, withoutBridge.takeIf { it.isNotEmpty() })
        waitUntil { BridgeAccessibility.current == null }
        val status = debugStatus()
        assertFalse(status.getBoolean("enabled"))
        assertTrue(status.isNull("debug_session"))
        unavailableConsentRefusal("screen.tap", args(session).put("x", point.first).put("y", point.second))
        assertEquals("Counter: 0", localText("counter"))
        restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, enabledServices)
        waitUntil { BridgeAccessibility.current != null }
        assertFalse(debugStatus().getBoolean("enabled"))
        unavailableConsentRefusal("screen.tap", args(session).put("x", point.first).put("y", point.second))
        assertEquals("Counter: 0", localText("counter"))
    }

    @Test fun secureAndPasswordWindowsRefuseCaptureAndEffects() {
        val session = localEnable()
        showFixture("secure")
        captureRefused(session)
        showFixture("password")
        refused(command("screen.tree", args(session)), "UNSUPPORTED")
        val bounds = Rect().also { localControl("$fixture:id/increment").getBoundsInScreen(it) }
        val point = Pair(bounds.centerX(), bounds.centerY())
        captureRefused(session)
        refused(command("screen.tap", args(session).put("x", point.first).put("y", point.second)), "UNSUPPORTED")
        refused(command("screen.input", args(session).put("resource_id", "$fixture:id/editor").put("text", "Must not change")), "UNSUPPORTED")
        assertEquals("Counter: 0", localText("counter"))
        assertEquals("Synthetic initial text", localText("editor"))
    }

    @Test fun serviceTeardownDoesNotRestoreIndefiniteConsent() {
        val old = localEnable()
        assertTrue(debugStatus().isNull("remaining_seconds"))
        scenario!!.onActivity { BridgeService.stop(it) }
        waitUntil { BridgeService.current == null }
        showMain()
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { http("GET", "/v1/devices/${deviceId!!}").optBoolean("online") }
        assertFalse(debugStatus().getBoolean("enabled"))
        showFixture()
        unavailableConsentRefusal("screen.tree", args(old))
    }

    private class NativeClockNotReady : RuntimeException()
    private data class NativeAlarm(val label: String, val time: String, val enabled: Boolean, val schedule: String)
    private fun nativeClockNodes(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = mutableListOf<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            check(depth <= 30 && result.size < 400 && node.childCount <= 100)
            if (!node.refresh()) throw NativeClockNotReady()
            check(node.packageName?.toString() == clockPackage && !node.isPassword)
            result.add(node)
            repeat(node.childCount) { visit(node.getChild(it) ?: throw NativeClockNotReady(), depth + 1) }
        }
        visit(root, 0)
        return result
    }
    private fun nativeClockRoot(): AccessibilityNodeInfo = automation.rootInActiveWindow
        ?.takeIf { it.packageName?.toString() == clockPackage } ?: throw NativeClockNotReady()
    private fun nativeOne(nodes: List<AccessibilityNodeInfo>, id: String): AccessibilityNodeInfo =
        nodes.single { it.viewIdResourceName == "$clockPackage:id/$id" && it.isVisibleToUser }
    private fun nativeAlarmCard(label: String): AccessibilityNodeInfo {
        var node = nativeClockNodes(nativeClockRoot()).single { it.viewIdResourceName == "$clockPackage:id/label" && it.text?.toString() == label && it.isVisibleToUser }
        repeat(30) {
            if (node.viewIdResourceName == "$clockPackage:id/alarm_card") return node
            node = node.parent ?: error("Genuine Clock alarm card ancestry missing")
        }
        error("Genuine Clock alarm card ancestry exceeded bound")
    }
    private fun nativeAlarms(): List<NativeAlarm> {
        val nodes = nativeClockNodes(nativeClockRoot())
        val collection = nodes.singleOrNull { it.viewIdResourceName == "$clockPackage:id/alarm_recycler_view" && it.isVisibleToUser } ?: throw NativeClockNotReady()
        val info = collection.collectionInfo ?: throw NativeClockNotReady()
        val cards = nodes.filter { it.viewIdResourceName == "$clockPackage:id/alarm_card" && it.isVisibleToUser }
        val count = info.rowCount * info.columnCount
        check(count in 0..8)
        if (cards.size != count) throw NativeClockNotReady()
        val indexes = cards.map { card ->
            var current = card
            var index: Int? = null
            repeat(30) {
                if (index == null) {
                    index = current.collectionItemInfo?.rowIndex
                    if (index == null) current = current.parent ?: throw NativeClockNotReady()
                }
            }
            index ?: throw NativeClockNotReady()
        }
        if (indexes.sorted() != (0 until count).toList()) throw NativeClockNotReady()
        return cards.map { card ->
            val row = nativeClockNodes(card)
            val time = nativeOne(row, "digital_clock")
            val toggle = nativeOne(row, "onoff")
            check(toggle.isCheckable && toggle.isEnabled)
            check(time.text?.toString() == time.contentDescription?.toString())
            val labels = row.filter { it.viewIdResourceName == "$clockPackage:id/label" && it.isVisibleToUser }
            val days = row.filter { it.viewIdResourceName == "$clockPackage:id/days_of_week" && it.isVisibleToUser }
            check(labels.size <= 1 && days.size <= 1)
            if (days.isEmpty()) check(nativeOne(row, "upcoming_instance_label").text?.toString() in setOf("Today", "Tomorrow", "Not scheduled"))
            NativeAlarm(labels.singleOrNull()?.text?.toString().orEmpty(), time.text.toString(), toggle.isChecked,
                days.singleOrNull()?.contentDescription?.toString().orEmpty())
        }.sortedWith(compareBy({ it.time }, { it.label }))
    }
    private fun wholeFlowPhase(phase: String) {
        wholeFlowPhases.add(phase)
        check(wholeFlowPhases.size <= 32)
        wholeFlowDirectory.mkdirs()
        val pending = File(wholeFlowDirectory, "progress.pending")
        pending.writeText(JSONObject().put("phases", org.json.JSONArray(wholeFlowPhases)).toString())
        check(pending.renameTo(File(wholeFlowDirectory, "progress.json")))
        waitUntil(15000) {
            val ack = File(wholeFlowDirectory, "progress-ack.json")
            ack.isFile && JSONObject(ack.readText()).optInt("observed_phases") == wholeFlowPhases.size
        }
    }
    private fun nativeHostIdentity(nodes: List<AccessibilityNodeInfo>): List<String> = nodes.filter {
        it.isVisibleToUser && it.viewIdResourceName?.substringAfter(":id/") in setOf("label", "digital_clock", "onoff", "days_of_week", "clock", "alarm_label", "repeat_days", "day_button_0", "day_button_1", "day_button_2", "day_button_3", "day_button_4", "day_button_5", "day_button_6")
    }.map { "${it.viewIdResourceName}|${it.text}|${it.contentDescription}|${it.isChecked}" }.sorted()
    private fun dismissKnownClockFixturePromotion() {
        repeat(3) {
            val applications = automation.windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            if (applications.size == 1) return
            assertEquals("Only a single exact genuine Clock promotion may be dismissed by fixture setup", 2, applications.size)
            assertTrue(applications.all { it.root?.packageName?.toString() == clockPackage })
            val host = applications.single { it.id == nativeClockRoot().windowId && it.isActive && it.isFocused }
            val popup = applications.single { it.id != host.id }
            assertFalse(popup.isActive || popup.isFocused)
            assertTrue(popup.layer > host.layer)
            val popupNodes = nativeClockNodes(popup.root ?: error("Clock promotion root missing"))
            val body = nativeOne(popupNodes, "body")
            val text = body.text?.toString().orEmpty()
            val music = Regex("You can now choose music from (Calm|Spotify|Pandora|YouTube Music), (Calm|Spotify|Pandora|YouTube Music), (Calm|Spotify|Pandora|YouTube Music), & (Calm|Spotify|Pandora|YouTube Music)").matchEntire(text)
            assertTrue(text in setOf("Set a consistent bedtime for better sleep", "You can find the privacy policy here") ||
                (music != null && music.groupValues.drop(1).toSet() == setOf("Calm", "Spotify", "Pandora", "YouTube Music") && nativeOne(popupNodes, "header").text?.toString() == "Wake up to music"))
            assertEquals("android.widget.TextView", body.className?.toString())
            assertFalse(popupNodes.any { it.isVisibleToUser && it.viewIdResourceName?.substringAfter(":id/") in setOf("alarm_card", "onoff", "delete_button", "save_button", "alarm_recycler_view", "clock", "alarm_label") })
            val before = nativeHostIdentity(nativeClockNodes(host.root ?: error("Clock host missing")))
            assertTrue(before.isNotEmpty())
            refused(command("clock.alarms.list", JSONObject().put("package", clockPackage)), "UNSUPPORTED")
            wholeFlowPhase("promotion_refused")
            val freshPopup = automation.windows.single { it.id == popup.id }
            val dismissal = nativeOne(nativeClockNodes(freshPopup.root ?: error("Clock popup disappeared")), "dismiss_button")
            assertEquals("Dismiss", dismissal.text?.toString())
            assertEquals("android.widget.TextView", dismissal.className?.toString())
            assertTrue(dismissal.isEnabled && dismissal.isClickable)
            val popupBounds = Rect().also { freshPopup.getBoundsInScreen(it) }
            val hostBounds = Rect().also { host.getBoundsInScreen(it) }
            val bounds = Rect().also { dismissal.getBoundsInScreen(it) }
            assertTrue(!bounds.isEmpty && popupBounds.contains(bounds) && hostBounds.contains(popupBounds))
            assertTrue(dismissal.performAction(AccessibilityNodeInfo.ACTION_CLICK))
            waitUntil { automation.windows.none { it.id == popup.id } }
            assertEquals("Explicit fixture dismissal must preserve native host alarm identities", before, nativeHostIdentity(nativeClockNodes(nativeClockRoot())))
            wholeFlowPhase("promotion_dismissed")
        }
        fail("Genuine Clock promotion did not settle after bounded exact fixture dismissals")
    }
    private fun showNativeAlarms(restart: Boolean = false) {
        if (restart) {
            shell("am force-stop $clockPackage")
            assertTrue("Clock process must actually stop before persistence readback", shell("pidof $clockPackage").trim().isEmpty())
        }
        dispatched(success(command("clock.alarms.show", JSONObject().put("package", clockPackage))))
        waitUntil { automation.rootInActiveWindow?.packageName?.toString() == clockPackage }
        Thread.sleep(500)
        dismissKnownClockFixturePromotion()
        waitUntil { try { nativeAlarms().size >= 2 } catch (_: NativeClockNotReady) { false } }
    }
    private fun httpNativeAlarms(): List<NativeAlarm> {
        val listed = success(command("clock.alarms.list", JSONObject().put("package", clockPackage)))
        assertTrue(listed.getBoolean("complete"))
        assertEquals("ui", listed.getString("backend"))
        val items = listed.getJSONArray("alarms")
        val result = (0 until items.length()).map { index ->
            val item = items.getJSONObject(index)
            NativeAlarm(item.getString("label"), item.getString("time"), item.getBoolean("enabled"), item.getString("schedule"))
        }.sortedWith(compareBy({ it.time }, { it.label }))
        assertEquals("HTTP adapter list must agree with independent genuine Clock UI/model readback", nativeAlarms(), result)
        return result
    }
    private fun expectNativeAlarms(expected: List<NativeAlarm>) {
        val sorted = expected.sortedWith(compareBy({ it.time }, { it.label }))
        waitUntil { try { nativeAlarms() == sorted } catch (_: NativeClockNotReady) { false } }
        assertEquals(sorted, httpNativeAlarms())
    }
    private fun genericClockTap(session: String, node: AccessibilityNodeInfo) {
        assertEquals(clockPackage, success(command("screen.tree", args(session, clockPackage))).getString("package"))
        assertTrue(node.refresh() && node.isVisibleToUser && node.isEnabled && !node.isPassword)
        val bounds = Rect().also { node.getBoundsInScreen(it) }
        assertTrue(!bounds.isEmpty)
        val result = success(command("screen.tap", args(session, clockPackage).put("x", bounds.centerX()).put("y", bounds.centerY())))
        dispatched(result)
        assertTrue(result.getBoolean("gesture_completed"))
    }
    private fun requestHostAdbToggle(session: String, target: NativeAlarm) {
        wholeFlowPhase("adb_requested")
        assertEquals(session, debugStatus().getString("debug_session"))
        val toggle = nativeOne(nativeClockNodes(nativeAlarmCard(target.label)), "onoff")
        assertTrue(toggle.isChecked == target.enabled && toggle.isVisibleToUser && toggle.isEnabled)
        val bounds = Rect().also { toggle.getBoundsInScreen(it) }
        val nonce = UUID.randomUUID().toString()
        val request = JSONObject().put("version", 1).put("nonce", nonce).put("debug_session", session)
            .put("expected_package", clockPackage).put("x", bounds.centerX()).put("y", bounds.centerY())
            .put("expires_elapsed_ms", SystemClock.elapsedRealtime() + 20000)
        val pending = File(wholeFlowDirectory, "request.pending")
        pending.writeText(request.toString())
        check(pending.renameTo(File(wholeFlowDirectory, "request.json")))
        val ack = File(wholeFlowDirectory, "ack.json")
        waitUntil(20000) { ack.isFile }
        val observed = JSONObject(ack.readText())
        assertEquals(nonce, observed.getString("nonce"))
        assertEquals(session, observed.getString("debug_session"))
        assertTrue(observed.getBoolean("dispatched"))
        assertEquals("host_adb_shell_input", observed.getString("transport"))
        assertEquals(session, debugStatus().getString("debug_session"))
        File(wholeFlowDirectory, "request.json").delete()
    }

    @Test fun realWholeFlowHttpWssGenuineClockHostAdbToggleAndRestartPreserveSurvivor() {
        assertEquals("true", InstrumentationRegistry.getArguments().getString("genuineClockWholeFlow"))
        wholeFlowDirectory.mkdirs()
        wholeFlowDirectory.listFiles()?.forEach { check(it.delete()) }
        originalTimeFormat = Settings.System.getString(context.contentResolver, Settings.System.TIME_12_24)
        clockFixtureActive = true
        try {
            assertTrue(shell("pm clear $clockPackage").contains("Success"))
            shell("settings put system time_12_24 24")
            shell("cmd appops set $clockPackage SCHEDULE_EXACT_ALARM allow")
            if (Build.VERSION.SDK_INT >= 33) shell("pm grant $clockPackage android.permission.POST_NOTIFICATIONS")
            assertTrue("Genuine pinned installed Google Clock bytes/version/signer are mandatory", GoogleClockProfile.installed(context))
            val session = localEnable("debug_enable_10")
            wholeFlowPhase("clock_ready")
            showNativeAlarms()
            val baseline = httpNativeAlarms()
            assertEquals(2, baseline.size)
            assertEquals(setOf("08:30", "09:00"), baseline.map { it.time }.toSet())
            assertTrue(baseline.all { !it.enabled && it.label.isEmpty() })
            wholeFlowPhase("baseline_listed")
            val suffix = UUID.randomUUID().toString().take(8)
            val future = Calendar.getInstance().apply { add(Calendar.HOUR_OF_DAY, 12) }
            val hour = future.get(Calendar.HOUR_OF_DAY)
            val target = NativeAlarm("PB target $suffix", String.format(Locale.US, "%02d:31", hour), true, "every day")
            val survivor = NativeAlarm("PB survivor $suffix", String.format(Locale.US, "%02d:43", hour), true, "every day")
            for ((alarm, phase) in listOf(target to "target_created", survivor to "survivor_created")) {
                val create = success(command("clock.alarms.create", JSONObject().put("package", clockPackage).put("hour", hour)
                    .put("minute", alarm.time.substringAfter(':').toInt()).put("label", alarm.label).put("days", org.json.JSONArray(listOf(1, 2, 3, 4, 5, 6, 7)))))
                dispatched(create)
                assertEquals("native_intent", create.getString("backend"))
                showNativeAlarms()
                expectNativeAlarms(baseline + target + if (alarm == survivor) listOf(survivor) else emptyList())
                wholeFlowPhase(phase)
            }
            val created = baseline + target + survivor
            showNativeAlarms(restart = true)
            expectNativeAlarms(created)
            wholeFlowPhase("created_restarted")
            requestHostAdbToggle(session, target)
            val disabled = baseline + target.copy(enabled = false) + survivor
            expectNativeAlarms(disabled)
            wholeFlowPhase("adb_observed")
            showNativeAlarms(restart = true)
            expectNativeAlarms(disabled)
            wholeFlowPhase("adb_restarted")
            for ((op, enabled, phase) in listOf(Triple("clock.alarms.enable", true, "enabled_restarted"), Triple("clock.alarms.disable", false, "disabled_restarted"), Triple("clock.alarms.enable", true, "reenabled_restarted"))) {
                val effect = success(command(op, JSONObject().put("package", clockPackage).put("label", target.label).put("time", target.time)))
                assertEquals("changed", effect.getString("outcome"))
                assertTrue("Only the actual Clock adapter may report its own verified readback", effect.getBoolean("verified"))
                expectNativeAlarms(baseline + target.copy(enabled = enabled) + survivor)
                showNativeAlarms(restart = true)
                expectNativeAlarms(baseline + target.copy(enabled = enabled) + survivor)
                wholeFlowPhase(phase)
            }
            genericClockTap(session, nativeAlarmCard(target.label))
            waitUntil { try { nativeClockNodes(nativeClockRoot()).any { it.viewIdResourceName == "$clockPackage:id/alarm_label" && it.isVisibleToUser } } catch (_: NativeClockNotReady) { false } }
            Thread.sleep(500)
            dismissKnownClockFixturePromotion()
            val detail = nativeClockNodes(nativeClockRoot())
            assertEquals(target.label, nativeOne(detail, "alarm_label").text?.toString())
            assertEquals(target.time, nativeOne(detail, "clock").text?.toString())
            for (day in 0..6) {
                val button = nativeOne(detail, "day_button_$day")
                assertTrue(button.isCheckable && button.isChecked)
            }
            wholeFlowPhase("debug_detail")
            val deletion = nativeOne(nativeClockNodes(nativeClockRoot()), "delete_button")
            assertEquals("Delete", deletion.text?.toString())
            genericClockTap(session, deletion)
            expectNativeAlarms(baseline + survivor)
            wholeFlowPhase("debug_deleted")
            showNativeAlarms(restart = true)
            expectNativeAlarms(baseline + survivor)
            wholeFlowPhase("deleted_restarted")
            wholeFlowPhase("completed")
        } catch (error: Throwable) {
            try { wholeFlowPhase("failed") } catch (evidenceError: Throwable) { error.addSuppressed(evidenceError) }
            throw error
        } finally {
            File(wholeFlowDirectory, "request.json").delete()
        }
    }
}
