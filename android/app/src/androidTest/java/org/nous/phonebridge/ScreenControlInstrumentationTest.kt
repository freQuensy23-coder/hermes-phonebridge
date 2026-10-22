package org.nous.phonebridge

import android.accessibilityservice.AccessibilityServiceInfo
import android.app.UiAutomation
import android.content.Context
import android.graphics.BitmapFactory
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.provider.Settings
import android.util.Base64
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
import org.junit.AfterClass
import org.junit.Assert.*
import org.junit.Before
import org.junit.BeforeClass
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.RunWith
import org.junit.runners.model.Statement
import java.util.concurrent.TimeUnit

/**
 * Pairing through the public tunnel, starting the service and the first hello take seconds, so the tests in this
 * class share one paired device and one Accessibility grant. [setup] re-checks that state before every test and
 * pairs a fresh device if an earlier test broke it; [tearDownClass] revokes the device and restores every setting,
 * so the class leaves nothing behind and still runs alone, in any order or on its own shard.
 */
@RunWith(AndroidJUnit4::class)
class ScreenControlInstrumentationTest {
    private var scenario: ActivityScenario<MainActivity>? = null

    @get:Rule val closeActivityRule = TestRule { base, _ ->
        object : Statement() {
            override fun evaluate() {
                var failure: Throwable? = null
                try { base.evaluate() } catch (error: Throwable) { failure = error }
                try { scenario?.close() } catch (error: Throwable) {
                    if (failure == null) failure = error else if (failure !== error) failure.addSuppressed(error)
                }
                scenario = null
                failure?.let { throw it }
            }
        }
    }

    @Before fun setup() {
        prepareDevice()
        scenario = ActivityScenario.launch(MainActivity::class.java)
        ensureConnected(scenario!!)
        scenario!!.onActivity { it.findViewById<android.widget.Button>(R.id.screen_enable_manual).performClick() }
        waitForScreenCapabilities(true)
    }

    @Test fun realHttpWsTreeCoordinateTapInputAndSingleSwipeWithIndependentReadback() {
        showFixture()
        val initial = tree()
        assertEquals(fixture, initial.getString("package"))
        assertEquals("Counter: 0", treeNode(initial, "counter").getString("text"))
        refused(command("screen.global", JSONObject().put("action", "unlock")), "VALIDATION_ERROR")
        assertEquals("Counter: 0", localText("counter"))
        val point = center(treeNode(initial, "increment"))
        val tap = success(command("screen.tap", JSONObject().put("x", point.first).put("y", point.second)))
        dispatched(tap)
        assertTrue(tap.getBoolean("gesture_completed"))
        waitUntil { localText("counter") == "Counter: 1" }
        val firstIncrement = SystemClock.elapsedRealtime()
        assertEquals("Counter: 1", treeNode(tree(), "counter").getString("text"))
        // One tap must increment exactly once: the counter has to stay at 1 for at least 300 ms after it changed.
        // The tree round trip above usually covers that window already; sleep only for whatever is left of it.
        (300 - (SystemClock.elapsedRealtime() - firstIncrement)).takeIf { it > 0 }?.let { Thread.sleep(it) }
        assertEquals("Counter: 1", localText("counter"))
        val requested = "Synthetic HTTP WSS input"
        val input = success(command("screen.input", JSONObject().put("resource_id", "$fixture:id/editor").put("text", requested)))
        assertTrue(input.getBoolean("verified"))
        waitUntil { localText("editor") == requested }
        assertEquals(requested, treeNode(tree(), "editor").getString("text"))
        val scroll = treeNode(tree(), "scroll").getJSONArray("bounds")
        val x = (scroll.getInt(0) + scroll.getInt(2)) / 2
        val top = scroll.getInt(1)
        val bottom = scroll.getInt(3)
        val y1 = bottom - (bottom - top) / 5
        val y2 = top + (bottom - top) / 5
        val before = localNodes().filter { it.isVisibleToUser && it.text?.startsWith("Synthetic scroll row") == true }.map { it.text.toString() }
        val swipe = success(command("screen.swipe", JSONObject().put("x1", x).put("y1", y1).put("x2", x).put("y2", y2).put("duration_ms", 400)))
        dispatched(swipe)
        assertTrue(swipe.getBoolean("gesture_completed"))
        waitUntil { localNodes().filter { it.isVisibleToUser && it.text?.startsWith("Synthetic scroll row") == true }.map { it.text.toString() } != before }
        assertEquals("Counter: 1", localText("counter"))
        // Last, because tapping the field opens the soft keyboard over the lower part of the screen.
        val editor = center(treeNode(tree(), "editor"))
        success(command("screen.tap", JSONObject().put("x", editor.first).put("y", editor.second)))
        waitUntil { localNodes().any { it.viewIdResourceName == "$fixture:id/editor" && it.isFocused } }
        val focused = "Typed into the focused field"
        success(command("screen.input", JSONObject().put("text", focused)))
        waitUntil { localText("editor") == focused }
    }

    @Test fun realPlatformScreenshotOfTheWholeDisplay() {
        showFixture()
        val image = success(command("screen.screenshot", JSONObject()))
        assertEquals("image/jpeg", image.getString("mime"))
        assertEquals(fixture, image.getString("package"))
        val display = context.getSystemService(WindowManager::class.java).maximumWindowMetrics.bounds
        val bounds = image.getJSONArray("screen_bounds")
        assertEquals(listOf(0, 0, display.width(), display.height()), (0 until 4).map { bounds.getInt(it) })
        val buttonBounds = treeNode(tree(), "increment").getJSONArray("bounds")
        assertTrue(buttonBounds.getInt(0) >= 0 && buttonBounds.getInt(1) >= 0)
        assertTrue(buttonBounds.getInt(2) <= display.width() && buttonBounds.getInt(3) <= display.height())
        val bytes = Base64.decode(image.getString("base64"), Base64.NO_WRAP)
        assertTrue(bytes.size in 1024..245760)
        assertEquals(0xff, bytes[0].toInt() and 255)
        assertEquals(0xd8, bytes[1].toInt() and 255)
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size) ?: error("Real JPEG must decode")
        try {
            assertEquals(image.getInt("width"), bitmap.width)
            assertEquals(image.getInt("height"), bitmap.height)
            assertTrue(maxOf(bitmap.width, bitmap.height) in 241..1280)
            assertTrue(kotlin.math.abs(bitmap.width.toDouble() / bitmap.height - display.width().toDouble() / display.height()) < 0.03)
            val pixels = mutableSetOf<Int>()
            for (y in 0 until bitmap.height step maxOf(1, bitmap.height / 24)) {
                for (x in 0 until bitmap.width step maxOf(1, bitmap.width / 24)) pixels.add(bitmap.getPixel(x, y))
            }
            assertTrue("Screenshot must contain nontrivial actual fixture pixels", pixels.size > 12)
        } finally { bitmap.recycle() }
    }

    @Test fun phoneBridgeOwnScreenCanBeReadAndOperated() {
        scenario!!.onActivity { it.findViewById<android.widget.EditText>(R.id.device_name).setText("Before") }
        waitUntil { automation.rootInActiveWindow?.packageName?.toString() == context.packageName }
        val own = tree()
        assertEquals(context.packageName, own.getString("package"))
        success(command("screen.input", JSONObject().put("resource_id", "${context.packageName}:id/device_name").put("text", "Renamed remotely")))
        var name = ""
        waitUntil { scenario!!.onActivity { name = it.findViewById<android.widget.EditText>(R.id.device_name).text.toString() }; name == "Renamed remotely" }
    }

    @Test fun stopButtonRemovesScreenOperationsAndTimedEnableRestoresThem() {
        scenario!!.onActivity { it.findViewById<android.widget.Button>(R.id.screen_stop).performClick() }
        waitForScreenCapabilities(false)
        val refused = request("POST", commandPath(), commandBody("screen.tree", JSONObject()))
        assertEquals(422, refused.status)
        assertEquals("UNSUPPORTED", refused.body.getJSONObject("error").getString("code"))
        scenario!!.onActivity { it.findViewById<android.widget.Button>(R.id.screen_enable_10).performClick() }
        waitForScreenCapabilities(true)
        showFixture()
        assertEquals(fixture, tree().getString("package"))
    }

    @Test fun accessibilityLossRemovesScreenOperations() {
        val enabledServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        val withoutBridge = enabledServices.orEmpty().split(':').filter { it.isNotEmpty() && it != component && it != "org.nous.phonebridge/.BridgeAccessibility" }.joinToString(":")
        restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, withoutBridge.takeIf { it.isNotEmpty() })
        waitUntil { BridgeAccessibility.current == null }
        waitForScreenCapabilities(false)
        restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, enabledServices)
        waitUntil { BridgeAccessibility.current != null }
        waitForScreenCapabilities(true)
        showFixture()
        assertEquals(fixture, tree().getString("package"))
    }

    companion object {
        private const val POLL_MS = 50L
        private const val fixture = "org.nous.phonebridge.debugfixture"
        private const val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
        private val instrumentation get() = InstrumentationRegistry.getInstrumentation()
        private val context: Context get() = instrumentation.targetContext
        private val automation: UiAutomation get() = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
        private val adminHttp = BridgeHttp.client.newBuilder().connectionPool(ConnectionPool(0, 1, TimeUnit.SECONDS)).retryOnConnectionFailure(false).callTimeout(15, TimeUnit.SECONDS).build()
        private lateinit var server: String
        private lateinit var publicServer: String
        private lateinit var adminToken: String
        private var deviceId: String? = null
        private var originalServices: String? = null
        private var originalEnabled: String? = null
        private var settingsCaptured = false
        private var originalAutomationFlags: Int? = null

        @JvmStatic @BeforeClass fun setUpClass() {
            val supplied = InstrumentationRegistry.getArguments()
            assertEquals("true", supplied.getString("emulatorCi"))
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
            retrieveInteractiveWindows()
            BridgeService.stop(context, true)
            waitUntil { BridgeService.current == null }
            originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
            originalEnabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED)
            settingsCaptured = true
            if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        }

        @JvmStatic @AfterClass fun tearDownClass() {
            var failure: Throwable? = null
            val id = deviceId
            val actions: List<() -> Unit> = listOf(
                { id?.let { http("DELETE", "/v1/devices/$it", expected = 204) }; Unit },
                { id?.let { assertEquals("NOT_FOUND", http("GET", "/v1/devices/$it", expected = 404).getJSONObject("error").getString("code")) }; Unit },
                { id?.let { assertNotListed(it) }; Unit },
                { deviceId = null },
                { BridgeService.stop(context, true) },
                { ScreenControl.stop(context) },
                { waitUntil { BridgeService.current == null && Credentials(context).load() == null } },
                { if (settingsCaptured) restoreSetting(Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES, originalServices) },
                { if (settingsCaptured) restoreSetting(Settings.Secure.ACCESSIBILITY_ENABLED, originalEnabled) },
                { settingsCaptured = false },
                { shell("am force-stop $fixture"); Unit },
                {
                    originalAutomationFlags?.let { flags ->
                        val serviceInfo = automation.serviceInfo ?: error("Independent automation service info unavailable during restore")
                        serviceInfo.flags = flags
                        automation.serviceInfo = serviceInfo
                        assertEquals(flags, automation.serviceInfo.flags)
                    }
                    originalAutomationFlags = null
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

        /** Awake, unlocked, PhoneBridge Accessibility granted and connected: re-applied before every test. */
        private fun prepareDevice() {
            retrieveInteractiveWindows()
            val services = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES).orEmpty().split(':').filter { it.isNotEmpty() }
            if (component !in services) shell("settings put secure enabled_accessibility_services ${quote(services.plus(component).distinct().joinToString(":"))}")
            if (Settings.Secure.getString(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED) != "1") shell("settings put secure accessibility_enabled 1")
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            waitUntil { BridgeAccessibility.current != null }
        }
        /** Brings the shared session to the state every test starts from; pairs a new device only when needed. */
        private fun ensureConnected(scenario: ActivityScenario<MainActivity>) {
            val id = deviceId
            if (id != null && BridgeService.current != null && Credentials(context).load()?.deviceId == id) {
                waitUntil { http("GET", "/v1/devices/$id").optBoolean("online") }
                return
            }
            if (id != null) {
                // An earlier test left the shared device unusable: revoke it and pair a fresh one.
                deviceId = null
                assertTrue("Stale shared device must be revocable", request("DELETE", "/v1/devices/$id").status in setOf(204, 404))
                assertNotListed(id)
                BridgeService.stop(context, true)
                waitUntil { BridgeService.current == null && Credentials(context).load() == null }
            }
            val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Screen control CI").put("ttl_seconds", 300))
            val payload = pairing.getJSONObject("qr_payload")
            assertEquals(publicServer, payload.getString("server"))
            val qr = PairingQr.parse(payload.toString(), false)
            val claimed = BridgeHttp.claim(qr, "Screen control CI", false)
            deviceId = claimed.deviceId
            assertEquals("$publicServer/", claimed.server)
            assertEquals("wss://${qr.server.host}/v1/device/ws", claimed.wsUrl)
            Credentials(context).save(claimed)
            assertEquals(claimed, Credentials(context).load())
            val plainPrefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE).all.values.joinToString()
            assertFalse(plainPrefs.contains(claimed.token))
            assertFalse(plainPrefs.contains(qr.token))
            scenario.onActivity { BridgeService.start(it) }
            waitUntil { http("GET", "/v1/devices/${claimed.deviceId}").optBoolean("online") }
            waitForScreenCapabilities(false)
        }
        private fun retrieveInteractiveWindows() {
            val serviceInfo = automation.serviceInfo ?: error("Independent automation service info unavailable")
            if (serviceInfo.flags and AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS == 0) {
                serviceInfo.flags = serviceInfo.flags or AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS
                automation.serviceInfo = serviceInfo
            }
            assertTrue(automation.serviceInfo.flags and AccessibilityServiceInfo.FLAG_RETRIEVE_INTERACTIVE_WINDOWS != 0)
        }
        private fun assertNotListed(id: String) {
            val devices = http("GET", "/v1/devices").getJSONArray("devices")
            assertFalse((0 until devices.length()).any { devices.getJSONObject(it).getString("id") == id })
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
                Thread.sleep(POLL_MS)
            }
            fail("Required CI condition did not become true before deadline")
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
            val expected = if (enabled) setOf("screen.tree", "screen.screenshot", "screen.tap", "screen.swipe", "screen.input", "screen.global") else emptySet()
            waitUntil { advertisedScreenOperations() == expected }
        }
        private fun success(value: JSONObject): JSONObject {
            assertEquals("Command must succeed through the production HTTP and WSS path: $value", "succeeded", value.getString("status"))
            return value.getJSONObject("data")
        }
        private fun refused(value: JSONObject, code: String) {
            assertEquals("failed", value.getString("status"))
            assertEquals(code, value.getJSONObject("error").getString("code"))
            assertFalse(value.has("data") && !value.isNull("data"))
        }
        private fun tree() = success(command("screen.tree", JSONObject()))
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
        /**
         * Shows a fresh fixture activity (NEW_TASK | CLEAR_TASK replaces any earlier instance, so the counter, text and
         * scroll position start over) and waits until the PhoneBridge service itself reads the fixture as the active
         * window, which is what the commands under test act on. A warm process starts much faster than a cold one;
         * [tearDownClass] force-stops the fixture.
         */
        private fun showFixture(mode: String = "ordinary") {
            shell("am start -W -f 0x10008000 -n $fixture/.FixtureActivity --es screen $mode")
            waitUntil {
                localText("counter") == "Counter: 0" && localText("editor") == "Synthetic initial text" &&
                    BridgeAccessibility.current?.rootInActiveWindow?.packageName?.toString() == fixture
            }
        }
        private fun dispatched(result: JSONObject) {
            assertEquals("dispatched", result.getString("outcome"))
            assertFalse(result.getBoolean("verified"))
        }
    }
}
