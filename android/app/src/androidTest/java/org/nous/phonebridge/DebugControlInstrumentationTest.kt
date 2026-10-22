package org.nous.phonebridge

import android.accessibilityservice.AccessibilityServiceInfo
import android.app.UiAutomation
import android.content.Context
import android.graphics.BitmapFactory
import android.graphics.Rect
import android.hardware.display.DisplayManager
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.provider.Settings
import android.util.Base64
import android.view.Display
import android.view.WindowInsets
import android.view.WindowManager
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
    private fun bridgeNavigationProofDiagnostic(): String? = try {
        UiThread.call {
            val service = BridgeAccessibility.current ?: return@call null
            val flags = MutableList(10) { 0 }
            fun probe() {
                val resource = service.resources.getIdentifier("config_recentsComponentName", "string", "android")
                if (resource == 0) return
                flags[0] = 1
                val component = android.content.ComponentName.unflattenFromString(service.resources.getString(resource)) ?: return
                flags[1] = 1
                val manager = service.packageManager
                val activity = manager.getActivityInfo(component, android.content.pm.PackageManager.MATCH_SYSTEM_ONLY)
                flags[2] = 1
                val application = activity.applicationInfo ?: return
                flags[3] = if (activity.packageName == component.packageName && application.packageName == component.packageName) 1 else 0
                flags[4] = if (activity.enabled && application.enabled) 1 else 0
                flags[5] = if (application.flags and android.content.pm.ApplicationInfo.FLAG_SYSTEM != 0) 1 else 0
                val enabled = setOf(android.content.pm.PackageManager.COMPONENT_ENABLED_STATE_DEFAULT, android.content.pm.PackageManager.COMPONENT_ENABLED_STATE_ENABLED)
                flags[6] = if (manager.getApplicationEnabledSetting(component.packageName) in enabled) 1 else 0
                flags[7] = if (manager.getComponentEnabledSetting(component) in enabled) 1 else 0
                flags[8] = if (manager.checkPermission("android.permission.STATUS_BAR_SERVICE", component.packageName) == android.content.pm.PackageManager.PERMISSION_GRANTED) 1 else 0
                flags[9] = if (service.windows.any { it.root?.packageName?.toString() == component.packageName }) 1 else 0
            }
            try { probe() } catch (_: Exception) { }
            flags.joinToString(",", "[", "]")
        }
    } catch (_: Exception) { null }
    private fun bridgeBarProofDiagnostic(): String? = try {
        UiThread.call {
            val service = BridgeAccessibility.current ?: return@call null
            fun geometry(): List<Int>? = try {
                val display = service.getSystemService(DisplayManager::class.java)?.getDisplay(Display.DEFAULT_DISPLAY)
                if (display == null || !display.isValid) null else {
                    val windowContext = service.createWindowContext(display, WindowManager.LayoutParams.TYPE_ACCESSIBILITY_OVERLAY, null)
                    val metrics = if (windowContext.display?.displayId == Display.DEFAULT_DISPLAY)
                        windowContext.getSystemService(WindowManager::class.java)?.maximumWindowMetrics else null
                    metrics?.let {
                        val bounds = it.bounds
                        val values = mutableListOf(bounds.left, bounds.top, bounds.right, bounds.bottom)
                        for (type in listOf(WindowInsets.Type.statusBars(), WindowInsets.Type.navigationBars(), WindowInsets.Type.displayCutout())) {
                            val insets = it.windowInsets.getInsetsIgnoringVisibility(type)
                            values.addAll(listOf(insets.left, insets.top, insets.right, insets.bottom))
                        }
                        values
                    }
                }
            } catch (_: Exception) { null }
            val geometry = geometry()
            val selectedOwner = service.rootInActiveWindow?.packageName?.toString()
            val windows = service.windows.take(16)
            val vector = mutableListOf(if (geometry == null) 0 else 1)
            vector.addAll(geometry ?: List(16) { 0 })
            vector.add(windows.size)
            for (window in windows) {
                val owner = window.root?.packageName?.toString()
                val category = when (owner) {
                    null -> 0
                    "com.android.systemui" -> 1
                    "android" -> 2
                    selectedOwner -> 3
                    else -> 4
                }
                vector.addAll(listOf(window.id, category))
            }
            vector.joinToString(",", "[", "]")
        }
    } catch (_: Exception) { null }
    private fun bridgeWindowsDiagnostic(): String? = try {
        UiThread.call {
            val service = BridgeAccessibility.current ?: return@call null
            fun bounds(node: AccessibilityNodeInfo?): List<Int> {
                if (node == null) return listOf(-1, -1, -1, -1)
                val rect = Rect().also { node.getBoundsInScreen(it) }
                return listOf(rect.left, rect.top, rect.right, rect.bottom)
            }
            val selected = service.rootInActiveWindow
            val windows = service.windows.take(16)
            val vector = mutableListOf(selected?.windowId ?: -1)
            vector.addAll(bounds(selected))
            vector.add(windows.size)
            for (window in windows) {
                val native = Rect().also { window.getBoundsInScreen(it) }
                vector.addAll(listOf(window.id, window.type, if (window.isActive) 1 else 0, if (window.isFocused) 1 else 0,
                    native.left, native.top, native.right, native.bottom))
                vector.addAll(bounds(window.root))
            }
            vector.joinToString(",", "[", "]")
        }
    } catch (_: Exception) { null }
    private fun success(value: JSONObject): JSONObject {
        val observedCode = value.optJSONObject("error")?.optString("code").orEmpty()
        val allowedCodes = setOf("UNSUPPORTED", "PERMISSION_REQUIRED", "FOREGROUND_REQUIRED", "PHONE_LOCKED", "AMBIGUOUS_STATE", "VALIDATION_ERROR", "NOT_FOUND", "EXPIRED", "INTERNAL_ERROR")
        val code = observedCode.takeIf { it in allowedCodes } ?: "UNCLASSIFIED"
        val reason = when (value.optJSONObject("error")?.optString("message")) {
            "System, input method and overlay windows are excluded" -> "SYSTEM_WINDOW"
            "Cannot inspect relevant window", "Cannot inspect selected window", "No accessible active window" -> "MISSING_WINDOW_ROOT"
            "Password-bearing windows are excluded" -> "PASSWORD_WINDOW"
            "Cannot inspect advertised window child", "Window exceeds complete password-guard traversal bound" -> "INCOMPLETE_TREE"
            "Relevant window has a different owner" -> "FOREIGN_OWNER"
            "Another window overlaps the selected native window" -> "OVERLAP"
            "PhoneBridge consent/settings UI cannot be remotely controlled", "PhoneBridge, permission, credential and lock screens are excluded" -> "OWN_UI"
            "Protected UI window is present" -> "PROTECTED_UI"
            "Selected window must be an active focused application with native bounds" -> "INVALID_ACTIVE_WINDOW"
            "No inspectable active window graph", "Selected active window is missing" -> "INVALID_WINDOW_GRAPH"
            "Clock window root unavailable", "Changed Clock window root unavailable" -> "CLOCK_MISSING_ROOT"
            "Clock promotion popup is not a collection host" -> "CLOCK_POPUP"
            "Clock window graph exceeds profile bounds", "Clock changed window graph exceeds profile bounds", "Clock modal-safe window graph is not recognized" -> "CLOCK_WINDOW_GRAPH"
            "Clock window root identity changed", "Clock tree contains stale-window nodes", "Clock window safety graph changed during fresh traversal" -> "CLOCK_WINDOW_CHANGED"
            "Clock tree exceeds complete traversal bound", "Clock refreshed child bound exceeded", "Clock tree child unavailable" -> "CLOCK_INCOMPLETE_TREE"
            "Clock node cannot be freshly reacquired" -> "CLOCK_STALE_NODE"
            "Clock tree contains excluded or foreign nodes" -> "CLOCK_FOREIGN_NODE"
            "Clock detail or loading surface is active" -> "CLOCK_DETAIL_OR_LOADING"
            "Clock complete collection anchor is unavailable" -> "CLOCK_COLLECTION_ANCHOR"
            "Clock complete collection count is unavailable" -> "CLOCK_COLLECTION_COUNT"
            "Clock alarm collection is not the source-bound GridLayout accessibility schema", "Unsupported Clock collection metadata", "Unsupported Clock collection item", "Unknown Clock collection rectangle" -> "CLOCK_COLLECTION_SCHEMA"
            "Complete bounded Clock collection is not exposed", "Clock collection contains unknown rows" -> "CLOCK_COLLECTION_INCOMPLETE"
            "Nonempty Clock collection is hidden", "Scrollable Clock collections are excluded", "Clock row is clipped" -> "CLOCK_COLLECTION_GEOMETRY"
            "Clock alarm identity is not independently corroborated", "Clock alarm full label is not corroborated", "Duplicate current Clock identities" -> "CLOCK_ALARM_IDENTITY"
            "Unknown Clock alarm card/switch schema" -> "CLOCK_ALARM_SCHEMA"
            "Unknown Clock alarm scheduling graph", "Unknown or date-scheduled Clock alarm", "Paused-date, watch, firing or routine alarms are excluded" -> "CLOCK_ALARM_SCHEDULE"
            else -> "UNCLASSIFIED"
        }
        val windows = if (value.optString("status") != "succeeded") bridgeWindowsDiagnostic()?.let { " bridge_windows=$it" }.orEmpty() else ""
        val barproof = if (value.optString("status") != "succeeded") bridgeBarProofDiagnostic()?.let { " bridge_barproof=$it" }.orEmpty() else ""
        val navigation = if (value.optString("status") != "succeeded") bridgeNavigationProofDiagnostic()?.let { " bridge_navproof=$it" }.orEmpty() else ""
        assertEquals("Command must succeed through the production HTTP and WSS path; bridge_code=$code bridge_reason=$reason$windows$barproof$navigation", "succeeded", value.getString("status"))
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
        val found = (0 until nodes.length()).map { nodes.getJSONObject(it) }.filter { it.optString("resource_id") == "$fixture:id/$name" }
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
        val launch = shell("am start -W -f 0x00020000 -n org.nous.phonebridge/.MainActivity")
        try {
            waitUntil { automation.rootInActiveWindow?.packageName?.toString() == context.packageName }
        } catch (error: AssertionError) {
            fun owner(node: AccessibilityNodeInfo?): Int = when (node?.packageName?.toString()) {
                null -> 0
                context.packageName -> 1
                "com.android.systemui" -> 2
                else -> 3
            }
            val flags = listOf(
                if (MainActivity.resumed) 1 else 0,
                if (context.getSystemService(android.os.PowerManager::class.java).isInteractive) 1 else 0,
                if (context.getSystemService(android.app.KeyguardManager::class.java).isKeyguardLocked) 1 else 0,
                owner(automation.rootInActiveWindow),
                UiThread.call { owner(BridgeAccessibility.current?.rootInActiveWindow) },
                if (context.packageName == "org.nous.phonebridge") 1 else 0,
                if (launch.contains("Error:")) 2 else if (launch.contains("Status: ok")) 1 else 0
            )
            val local = flags.joinToString(",", "[", "]")
            val windows = bridgeWindowsDiagnostic()?.let { " bridge_windows=$it" }.orEmpty()
            throw AssertionError("Local MainActivity observation failed; bridge_reason=LOCAL_MAIN bridge_local=$local$windows", error)
        }
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
}
