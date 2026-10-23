package org.nous.phonebridge

import android.app.UiAutomation
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.LinearGradient
import android.graphics.Paint
import android.graphics.Rect
import android.graphics.Shader
import android.os.Build
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.provider.Settings
import android.util.Base64
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.assertEquals
import org.junit.Assert.fail
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class ScreenshotTourTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val automation get() = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES)
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private val fixture = "org.nous.phonebridge.debugfixture"
    private val directory = "/sdcard/Download/phonebridge-tour"
    private lateinit var server: String
    private lateinit var publicServer: String
    private lateinit var adminToken: String
    private lateinit var pairingCode: String
    private var deviceId: String? = null
    private var originalServices: String? = null
    private var originalEnabled: String? = null
    private val delivered = mutableListOf<android.net.Uri>()
    private var scenario: ActivityScenario<MainActivity>? = null
    private val admin = BridgeHttp.client.newBuilder().connectionPool(okhttp3.ConnectionPool(0, 1, java.util.concurrent.TimeUnit.SECONDS)).callTimeout(60, java.util.concurrent.TimeUnit.SECONDS).build()

    private fun shell(command: String): String = automation.executeShellCommand(command).use { ParcelFileDescriptor.AutoCloseInputStream(it).bufferedReader().use { reader -> reader.readText() } }
    private fun writeFile(path: String, bytes: ByteArray) {
        val descriptors = automation.executeShellCommandRw("dd of=$path")
        ParcelFileDescriptor.AutoCloseOutputStream(descriptors[1]).use { it.write(bytes) }
        ParcelFileDescriptor.AutoCloseInputStream(descriptors[0]).use { it.readBytes() }
    }
    private fun snap(name: String) {
        instrumentation.waitForIdleSync()
        try { automation.waitForIdle(300, 3000) } catch (_: java.util.concurrent.TimeoutException) { }
        shell("screencap -p $directory/$name.png")
    }
    private fun awaitActiveWindow(pkg: String) {
        val deadline = SystemClock.elapsedRealtime() + 3000
        while (SystemClock.elapsedRealtime() < deadline) {
            if (try { automation.rootInActiveWindow?.packageName?.toString() == pkg } catch (_: Exception) { false }) return
            Thread.sleep(50)
        }
        android.util.Log.w("ScreenshotTour", "Active window did not become $pkg within 3 s")
    }
    private fun waitUntil(timeoutMs: Long = 45000, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (try { predicate() } catch (_: Exception) { false }) return
            Thread.sleep(50)
        }
        fail("Screenshot tour condition was not reached")
    }
    private fun http(method: String, path: String, body: JSONObject? = null): JSONObject {
        val request = Request.Builder().url(server + path).header("Authorization", "Bearer $adminToken").header("Connection", "close")
            .method(method, body?.toString()?.toRequestBody("application/json".toMediaType())).build()
        return admin.newCall(request).execute().use { response ->
            val text = response.body?.string().orEmpty()
            if (!response.isSuccessful) fail("HTTP $method $path returned ${response.code}: $text")
            if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }
    private fun finished(id: String): JSONObject {
        var result = JSONObject()
        waitUntil { result = http("GET", "/v1/commands/$id"); result.getString("status") in setOf("succeeded", "failed", "expired") }
        assertEquals("Command $id: $result", "succeeded", result.getString("status"))
        return result.getJSONObject("data")
    }
    private fun command(op: String, args: JSONObject): JSONObject =
        finished(http("POST", "/v1/devices/$deviceId/commands", JSONObject().put("op", op).put("args", args).put("ttl_seconds", 60)).getString("id"))
    private fun send(name: String, mime: String, kind: String, bytes: ByteArray): String {
        val digest = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
        val url = "$server/v1/devices/$deviceId/files".toHttpUrl().newBuilder().addQueryParameter("name", name)
            .addQueryParameter("mime", mime).addQueryParameter("kind", kind).build()
        val request = Request.Builder().url(url).header("Authorization", "Bearer $adminToken").header("Connection", "close").header("X-Content-SHA256", digest)
            .header("Idempotency-Key", UUID.randomUUID().toString()).post(bytes.toRequestBody("application/octet-stream".toMediaType())).build()
        val queued = admin.newCall(request).execute().use { JSONObject(it.body!!.string()) }
        return queued.getString("id")
    }
    private fun received(command: String): JSONObject =
        finished(command).also { result -> result.optString("uri").takeIf { it.isNotEmpty() }?.let { delivered += android.net.Uri.parse(it) } }
    private fun nodes(): List<AccessibilityNodeInfo> {
        val result = mutableListOf<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            if (depth > 40 || result.size >= 600) return
            result.add(node)
            repeat(node.childCount) { node.getChild(it)?.let { child -> visit(child, depth + 1) } }
        }
        automation.rootInActiveWindow?.let { visit(it, 0) }
        return result
    }
    private fun scroll(forward: Boolean): Boolean =
        nodes().firstOrNull { it.isScrollable }?.performAction(if (forward) AccessibilityNodeInfo.ACTION_SCROLL_FORWARD else AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD) == true
    private fun scrollTop() { repeat(20) { if (!scroll(false)) return; Thread.sleep(100) } }
    private fun reveal(id: String): AccessibilityNodeInfo {
        scenario!!.onActivity { it.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE) }
        scrollTop()
        repeat(30) {
            nodes().singleOrNull { it.viewIdResourceName == id && it.isVisibleToUser }?.let { node ->
                val bounds = Rect().also { node.getBoundsInScreen(it) }
                if (!bounds.isEmpty) return node
            }
            scroll(true)
            Thread.sleep(200)
        }
        fail("Control $id never became visible")
        error("unreachable")
    }
    private fun tap(node: AccessibilityNodeInfo) {
        val bounds = Rect().also { node.getBoundsInScreen(it) }
        shell("input tap ${bounds.centerX()} ${bounds.centerY()}")
        instrumentation.waitForIdleSync()
    }
    private fun showMain() {
        if (scenario == null) scenario = ActivityScenario.launch(MainActivity::class.java)
        else shell("am start -W -f 0x00020000 -n org.nous.phonebridge/.MainActivity")
        waitUntil { automation.rootInActiveWindow?.packageName?.toString() == context.packageName }
        scenario!!.onActivity { it.window.clearFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE) }
        scrollTop()
    }
    private fun notificationShade(name: String) {
        shell("cmd statusbar expand-notifications")
        awaitActiveWindow("com.android.systemui")
        snap(name)
        shell("cmd statusbar collapse")
        awaitActiveWindow(context.packageName)
    }
    private fun sampleImage(): ByteArray {
        val bitmap = Bitmap.createBitmap(960, 640, Bitmap.Config.ARGB_8888)
        val canvas = Canvas(bitmap)
        canvas.drawRect(0f, 0f, 960f, 640f, Paint().apply { shader = LinearGradient(0f, 0f, 960f, 640f, Color.rgb(38, 70, 160), Color.rgb(240, 120, 60), Shader.TileMode.CLAMP) })
        val text = Paint(Paint.ANTI_ALIAS_FLAG).apply { color = Color.WHITE; textSize = 72f; isFakeBoldText = true }
        canvas.drawText("Sent by Hermes", 80f, 300f, text)
        canvas.drawText("via PhoneBridge", 80f, 400f, text.apply { textSize = 56f; isFakeBoldText = false })
        return ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }.toByteArray()
    }

    @Before fun setup() {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue(arguments.getString("screenshotTour") == "true")
        server = arguments.getString("serverUrl") ?: error("serverUrl required")
        publicServer = arguments.getString("expectedPublicServerUrl") ?: error("expectedPublicServerUrl required")
        adminToken = arguments.getString("adminToken") ?: error("adminToken required")
        pairingCode = arguments.getString("pairingCode") ?: error("pairingCode from phonebridge pair --code required")
        BridgeService.stop(context, true)
        waitUntil { BridgeService.current == null }
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        originalEnabled = Settings.Secure.getString(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED)
        shell("settings delete secure enabled_accessibility_services")
        waitUntil { BridgeAccessibility.current == null }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        context.getSystemService(android.app.NotificationManager::class.java).cancelAll()
        shell("rm -rf $directory")
        shell("mkdir -p $directory")
    }

    @After fun cleanup() {
        if (!::server.isInitialized) return
        var failure: Throwable? = null
        val actions: List<() -> Unit> = listOf(
            { deviceId?.let { id -> try { http("DELETE", "/v1/devices/$id") } catch (_: Throwable) { } }; Unit },
            { BridgeService.stop(context, true) },
            { ScreenControl.stop(context) },
            {
                val services = originalServices
                shell(if (services.isNullOrEmpty()) "settings delete secure enabled_accessibility_services" else "settings put secure enabled_accessibility_services $services")
                val enabled = originalEnabled
                shell(if (enabled == null) "settings delete secure accessibility_enabled" else "settings put secure accessibility_enabled $enabled")
                Unit
            },
            { shell("am force-stop $fixture"); Unit },
            { scenario?.close(); scenario = null },
            { delivered.forEach { context.contentResolver.delete(it, null, null) }; delivered.clear() },
            { context.getSystemService(android.app.NotificationManager::class.java).cancelAll() }
        )
        for (action in actions) {
            try { action() } catch (error: Throwable) {
                if (failure == null) failure = error else if (failure !== error) failure.addSuppressed(error)
            }
        }
        failure?.let { throw it }
    }

    @Test fun captureOwnerAndAgentViews() {
        showMain()
        snap("01-not-paired")

        scenario!!.onActivity { activity -> activity.findViewById<android.widget.EditText>(R.id.pairing_code).setText(pairingCode) }
        val connect = reveal("org.nous.phonebridge:id/pair_with_code")
        snap("01b-code-pasted")
        tap(connect)
        waitUntil { nodes().any { it.viewIdResourceName == "android:id/button1" && it.isVisibleToUser } }
        snap("02-pair-with-code")
        tap(nodes().single { it.viewIdResourceName == "android:id/button1" })
        waitUntil { Credentials(context).load() != null }
        val claimed = Credentials(context).load()!!
        deviceId = claimed.deviceId
        waitUntil { http("GET", "/v1/devices/${claimed.deviceId}").optBoolean("online") }
        showMain()
        waitUntil { nodes().any { it.text?.toString()?.startsWith("Connected to Hermes") == true } }
        snap("03-connected")

        shell("settings put secure enabled_accessibility_services $component")
        shell("settings put secure accessibility_enabled 1")
        waitUntil { BridgeAccessibility.current != null }
        showMain()
        waitUntil { nodes().any { it.viewIdResourceName == "org.nous.phonebridge:id/screen_enable_10" && it.isVisibleToUser } }
        snap("04-accessibility-granted")
        tap(reveal("org.nous.phonebridge:id/screen_enable_10"))
        waitUntil { nodes().any { it.text?.toString()?.startsWith("On ·") == true } }
        snap("05-screen-control-on")
        waitUntil { http("GET", "/v1/devices/${claimed.deviceId}").getJSONArray("capabilities").toString().contains("screen.tap") }
        notificationShade("06-ongoing-notification")

        val imageCommand = send("hermes-photo.png", "image/png", "image", sampleImage())
        val documentCommand = send("meeting-notes.txt", "text/plain", "file", "Agenda\n1. Ship PhoneBridge MVP\n2. Review screenshots\n".toByteArray())
        val image = received(imageCommand)
        val document = received(documentCommand)
        writeFile("$directory/07-delivery-results.json", JSONObject().put("image", image).put("document", document).toString(2).toByteArray())
        notificationShade("07-files-received")

        shell("am start -W -n $fixture/.FixtureActivity")
        waitUntil { automation.rootInActiveWindow?.packageName?.toString() == fixture }
        snap("08-app-on-phone")
        val tree = command("screen.tree", JSONObject())
        writeFile("$directory/09-agent-screen-tree.json", tree.toString(2).toByteArray())
        val capture = command("screen.screenshot", JSONObject())
        writeFile("$directory/10-agent-screenshot.jpg", Base64.decode(capture.getString("base64"), Base64.NO_WRAP))
        val nodes = tree.getJSONArray("nodes")
        val button = (0 until nodes.length()).map { nodes.getJSONObject(it) }.single { it.optString("resource_id") == "$fixture:id/increment" }
        val bounds = button.getJSONArray("bounds")
        command("screen.tap", JSONObject()
            .put("x", (bounds.getInt(0) + bounds.getInt(2)) / 2).put("y", (bounds.getInt(1) + bounds.getInt(3)) / 2))
        waitUntil { nodes().any { it.text?.toString() == "Counter: 1" } }
        snap("11-after-agent-tap")
    }
}
