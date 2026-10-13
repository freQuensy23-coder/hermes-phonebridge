package org.nous.phonebridge

import android.Manifest
import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.content.Context
import android.content.ClipData
import android.content.ClipboardManager
import android.os.Build
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class ServerInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private lateinit var server: String
    private lateinit var adminToken: String
    private var deviceId: String? = null
    private var scenario: ActivityScenario<MainActivity>? = null
    private var recoverySocket: WebSocket? = null
    private val listener = "org.nous.phonebridge/org.nous.phonebridge.BridgeNotifications"
    private fun shell(command: String): String = instrumentation.uiAutomation.executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { it.readText() } }
    private fun http(method: String, path: String, body: JSONObject? = null, admin: Boolean = true, expected: Int = 200): JSONObject {
        val builder = Request.Builder().url(server + path)
        if (admin) builder.header("Authorization", "Bearer $adminToken")
        val payload = body?.toString()?.toRequestBody("application/json".toMediaType())
        builder.method(method, payload)
        return BridgeHttp.client.newCall(builder.build()).execute().use {
            assertEquals("Unexpected status for $method $path", expected, it.code)
            val text = it.body?.string().orEmpty()
            if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }
    private fun waitUntil(timeoutMs: Long = 30000, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(650)
        }
        fail("Condition not met within bounded timeout")
    }
    private fun online(id: String) = http("GET", "/v1/devices/$id").let { it.optBoolean("online") && it.optString("version") == "0.1.0" }
    private fun capabilities(id: String): Set<String> {
        val array = http("GET", "/v1/devices/$id").getJSONArray("capabilities")
        return (0 until array.length()).map { array.getString(it) }.toSet()
    }
    private fun enqueue(id: String, op: String, args: JSONObject = JSONObject(), ttl: Int = 60, key: String? = null): JSONObject {
        val payload = JSONObject().put("op", op).put("args", args).put("ttl_seconds", ttl)
        key?.let { payload.put("idempotency_key", it) }
        return http("POST", "/v1/devices/$id/commands", payload)
    }
    private fun terminal(id: String): JSONObject {
        var value = JSONObject()
        waitUntil { value = http("GET", "/v1/commands/$id"); value.getString("status") in setOf("succeeded", "failed", "expired") }
        return value
    }
    @Before fun setup() {
        val args = InstrumentationRegistry.getArguments()
        server = args.getString("serverUrl")?.trimEnd('/') ?: error("Automatic CI must supply serverUrl")
        adminToken = args.getString("adminToken") ?: error("Automatic CI must supply adminToken")
        assertEquals("http://10.0.2.2:18940", server)
        assertTrue(adminToken.length >= 32)
        BridgeService.stop(context, true)
        waitUntil(5000) { BridgeService.current == null }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        shell("cmd notification disallow_listener $listener")
        shell("cmd notification disallow_dnd ${context.packageName}")
        waitUntil(5000) { BridgeNotifications.current == null }
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        assertEquals("ok", http("GET", "/healthz", admin = false).getString("status"))
    }
    @After fun cleanup() {
        recoverySocket?.cancel()
        scenario?.close()
        BridgeService.stop(context, true)
        context.getSystemService(NotificationManager::class.java).cancel(777)
        shell("cmd notification disallow_listener $listener")
        shell("cmd notification disallow_dnd ${context.packageName}")
        deviceId?.let { id ->
            BridgeHttp.client.newCall(Request.Builder().url("$server/v1/devices/$id").header("Authorization", "Bearer $adminToken").delete().build()).execute().close()
        }
    }
    @Test fun realPairingKeystoreWebSocketPermissionsEventsReconnectExpiryAndRevocation() {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Android CI").put("ttl_seconds", 300))
        val qr = PairingQr.parse(pairing.getJSONObject("qr_payload").toString(), true)
        assertEquals("10.0.2.2", qr.server.host)
        val value = BridgeHttp.claim(qr, "Android CI API ${Build.VERSION.SDK_INT}", true)
        deviceId = value.deviceId
        val id = value.deviceId
        val status = http("GET", "/v1/pairings/${pairing.getString("id")}")
        assertEquals("claimed", status.getString("status"))
        assertEquals(id, status.getString("device_id"))
        try { BridgeHttp.claim(qr, "Duplicate", true); fail("One-time claim must be refused") } catch (e: BridgeFailure) { assertEquals("PAIRING_FAILED", e.code) }
        val credentials = Credentials(context)
        credentials.save(value)
        assertEquals(value, credentials.load())
        val plainPrefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(plainPrefs.contains(value.token))
        assertFalse(plainPrefs.contains(qr.token))
        scenario = ActivityScenario.launch(MainActivity::class.java)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { online(id) }
        assertTrue("device.info" in capabilities(id))
        assertFalse("notifications.list" in capabilities(id))
        waitUntil { !http("GET", "/v1/devices/$id").getJSONObject("permissions").getBoolean("notification_policy") }
        shell("cmd notification allow_dnd ${context.packageName}")
        waitUntil { http("GET", "/v1/devices/$id").getJSONObject("permissions").getBoolean("notification_policy") }
        assertFalse(http("GET", "/v1/devices/$id").getJSONObject("permissions").getBoolean("notification_listener"))
        shell("cmd notification disallow_dnd ${context.packageName}")
        waitUntil { !http("GET", "/v1/devices/$id").getJSONObject("permissions").getBoolean("notification_policy") }
        DurableStore(context).use { store ->
            try { NativeHandlers(context, store).execute("notifications.list", JSONObject()); fail("Listener permission required") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
        }
        val command = enqueue(id, "device.info", key = "device-info-dedup")
        val duplicate = enqueue(id, "device.info", key = "device-info-dedup")
        assertEquals(command.getString("id"), duplicate.getString("id"))
        val result = terminal(command.getString("id"))
        assertEquals("succeeded", result.getString("status"))
        assertEquals(Build.MODEL, result.getJSONObject("data").getString("model"))
        assertEquals(Build.VERSION.SDK_INT, result.getJSONObject("data").getInt("sdk"))
        shell("cmd notification allow_listener $listener")
        waitUntil { BridgeNotifications.current != null && "notifications.list" in capabilities(id) }
        val manager = context.getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(NotificationChannel("ci.notifications", "CI fixture", NotificationManager.IMPORTANCE_DEFAULT))
        manager.notify(777, Notification.Builder(context, "ci.notifications").setSmallIcon(android.R.drawable.ic_dialog_info).setContentTitle("CI permission notification").setContentText("Untrusted fixture content").build())
        var key: String? = null
        waitUntil {
            val list = terminal(enqueue(id, "notifications.list", JSONObject().put("limit", 8)).getString("id"))
            assertEquals("succeeded", list.getString("status"))
            val items = list.getJSONObject("data").getJSONArray("notifications")
            key = (0 until items.length()).map { items.getJSONObject(it) }.firstOrNull { it.optString("title") == "CI permission notification" }?.getString("key")
            key != null
        }
        val dismissal = terminal(enqueue(id, "notifications.dismiss", JSONObject().put("key", key)).getString("id"))
        assertEquals("succeeded", dismissal.getString("status"))
        assertFalse(dismissal.getJSONObject("data").getBoolean("verified"))
        waitUntil { BridgeNotifications.current?.activeNotifications?.none { it.key == key } == true }
        shell("cmd notification disallow_listener $listener")
        waitUntil { BridgeNotifications.current == null && "notifications.list" !in capabilities(id) }
        assertFalse(http("GET", "/v1/devices/$id").getJSONObject("permissions").getBoolean("notification_listener"))
        val eventId = DurableStore(context).use { it.event("ci.native_event", JSONObject().put("untrusted", true).put("value", "real durable outbox")) }
        BridgeService.refresh()
        waitUntil {
            val items = http("GET", "/v1/devices/$id/events?after=0&limit=100").getJSONArray("events")
            (0 until items.length()).any { items.getJSONObject(it).getString("id") == eventId }
        }
        waitUntil { DurableStore(context).use { it.events().none { event -> event.getString("id") == eventId } } }
        val connections = context.getSharedPreferences("connection", Context.MODE_PRIVATE).getLong("connections", 0)
        BridgeService.current!!.interruptTransport()
        waitUntil { context.getSharedPreferences("connection", Context.MODE_PRIVATE).getLong("connections", 0) > connections && context.getSharedPreferences("connection", Context.MODE_PRIVATE).getString("status", "") == "Connected" && online(id) }
        val afterReconnect = terminal(enqueue(id, "device.info").getString("id"))
        assertEquals("succeeded", afterReconnect.getString("status"))
        scenario!!.onActivity { BridgeService.stop(it) }
        waitUntil { BridgeService.current == null && !http("GET", "/v1/devices/$id").getBoolean("online") }
        val expired = enqueue(id, "device.info", ttl = 1)
        Thread.sleep(1500)
        assertEquals("expired", terminal(expired.getString("id")).getString("status"))
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { online(id) }
        assertEquals("expired", http("GET", "/v1/commands/${expired.getString("id")}").getString("status"))
        http("DELETE", "/v1/devices/$id", expected = 204)
        waitUntil { Credentials(context).load() == null && BridgeService.current == null }
        assertFalse(context.getSharedPreferences("connection", Context.MODE_PRIVATE).getBoolean("enabled", true))
        val devices = http("GET", "/v1/devices").getJSONArray("devices")
        assertFalse((0 until devices.length()).any { devices.getJSONObject(it).getString("id") == id })
    }
    private fun realServerRecovery(completed: Boolean) {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Result recovery CI").put("ttl_seconds", 300))
        val qr = PairingQr.parse(pairing.getJSONObject("qr_payload").toString(), true)
        val credentials = BridgeHttp.claim(qr, "Result recovery API ${Build.VERSION.SDK_INT}", true)
        deviceId = credentials.deviceId
        Credentials(context).save(credentials)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        val clipboard = context.getSystemService(ClipboardManager::class.java)
        val baseline = "before execution ${UUID.randomUUID()}"
        val requested = "execute once ${UUID.randomUUID()}"
        scenario!!.onActivity { clipboard.setPrimaryClip(ClipData.newPlainText("CI", baseline)) }
        val incoming = ArrayBlockingQueue<JSONObject>(8)
        val opened = CountDownLatch(1)
        recoverySocket = BridgeHttp.client.newWebSocket(Request.Builder().url(credentials.wsUrl).header("Authorization", "Bearer ${credentials.token}").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                if (webSocket.send(JSONObject().put("type", "hello").put("version", BuildConfig.VERSION_NAME).put("capabilities", Access.capabilities(context)).put("permissions", Access.permissions(context)).toString())) opened.countDown()
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                val frame = JSONObject(text)
                if (frame.optString("type") in setOf("command", "error")) incoming.offer(frame)
            }
        })
        assertTrue(opened.await(10, TimeUnit.SECONDS))
        waitUntil { online(credentials.deviceId) && "clipboard.set" in capabilities(credentials.deviceId) }
        val queued = enqueue(credentials.deviceId, "clipboard.set", JSONObject().put("text", requested))
        val frame = incoming.poll(10, TimeUnit.SECONDS) ?: error("Real server did not deliver the recovery command")
        assertEquals("command", frame.getString("type"))
        assertEquals(queued.getString("id"), frame.getString("id"))
        assertEquals("clipboard.set", frame.getString("op"))
        assertEquals(requested, frame.getJSONObject("args").getString("text"))
        var expected: JSONObject? = null
        val unknownId = UUID.randomUUID().toString()
        DurableStore(context).use { store ->
            assertNull(store.begin(frame))
            if (completed) {
                val data = NativeHandlers(context, store).execute(frame.getString("op"), frame.getJSONObject("args"))
                assertTrue(data.getBoolean("verified"))
                expected = Results.ok(frame.getString("id"), data)
                store.complete(frame.getString("id"), expected!!)
            }
            assertNull(store.begin(JSONObject().put("type", "command").put("id", unknownId).put("op", "device.info").put("args", JSONObject()).put("expires_at", frame.getLong("expires_at"))))
        }
        assertEquals("sent", http("GET", "/v1/commands/${frame.getString("id")}").getString("status"))
        recoverySocket!!.cancel()
        recoverySocket = null
        waitUntil { !http("GET", "/v1/devices/${credentials.deviceId}").getBoolean("online") }
        val afterLoss = if (completed) "changed after result send loss ${UUID.randomUUID()}" else baseline
        scenario!!.onActivity {
            assertEquals(if (completed) requested else baseline, clipboard.primaryClip!!.getItemAt(0).text.toString())
            if (completed) clipboard.setPrimaryClip(ClipData.newPlainText("CI", afterLoss))
            BridgeService.start(it)
        }
        waitUntil { online(credentials.deviceId) }
        val result = terminal(frame.getString("id"))
        if (completed) {
            assertEquals("succeeded", result.getString("status"))
            assertEquals(DurableStore.canonical(expected!!.getJSONObject("data")), DurableStore.canonical(result.getJSONObject("data")))
        } else {
            assertEquals("failed", result.getString("status"))
            assertEquals("AMBIGUOUS_STATE", result.getJSONObject("error").getString("code"))
        }
        scenario!!.onActivity { assertEquals(afterLoss, clipboard.primaryClip!!.getItemAt(0).text.toString()) }
        DurableStore(context).use { store ->
            val cached = store.begin(frame)!!
            assertEquals(completed, cached.getBoolean("ok"))
            val unknown = store.results().single { it.getString("id") == unknownId }
            assertEquals("AMBIGUOUS_STATE", unknown.getJSONObject("error").getString("code"))
        }
        assertEquals("succeeded", terminal(enqueue(credentials.deviceId, "device.info").getString("id")).getString("status"))
        assertTrue(online(credentials.deviceId))
        assertNotNull(BridgeService.current)
    }
    @Test fun completedClipboardResultLostBeforeSendIsReturnedOnlyOnRealServerReconnectReplay() = realServerRecovery(true)
    @Test fun recoveredReservationReportsAmbiguityOnRealServerReplayWithoutClipboardExecutionOrUnknownResultFlood() = realServerRecovery(false)
    @Test fun expiredServerGeneratedQrCannotBeClaimedOrParsed() {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Expiry CI").put("ttl_seconds", 1))
        val payload = pairing.getJSONObject("qr_payload")
        Thread.sleep(1600)
        assertEquals("expired", http("GET", "/v1/pairings/${pairing.getString("id")}").getString("status"))
        try { PairingQr.parse(payload.toString(), true); fail("Expired QR must fail") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
        val claim = JSONObject().put("token", payload.getString("token")).put("name", "Expired").put("platform", "android")
        BridgeHttp.client.newCall(Request.Builder().url("$server/v1/pair/claim").post(claim.toString().toRequestBody("application/json".toMediaType())).build()).execute().use { assertTrue(it.code in 400..499) }
    }
}
