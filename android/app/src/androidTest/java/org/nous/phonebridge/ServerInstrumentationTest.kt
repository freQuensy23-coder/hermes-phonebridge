package org.nous.phonebridge

import android.content.Context
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.io.ByteArrayOutputStream
import java.security.MessageDigest
import java.util.UUID
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class ServerInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private lateinit var server: String
    private lateinit var expectedPublicServerUrl: String
    private lateinit var adminToken: String
    private var deviceId: String? = null
    private var scenario: ActivityScenario<MainActivity>? = null
    private var recoverySocket: WebSocket? = null
    private val delivered = mutableListOf<Uri>()
    private val adminHttp = BridgeHttp.client.newBuilder().connectionPool(okhttp3.ConnectionPool(0, 1, TimeUnit.SECONDS)).build()
    private fun shell(command: String): String = instrumentation.uiAutomation.executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { it.readText() } }
    private fun http(method: String, path: String, body: JSONObject? = null, admin: Boolean = true, expected: Int = 200): JSONObject {
        val builder = Request.Builder().url(server + path)
        if (admin) builder.header("Authorization", "Bearer $adminToken")
        val payload = body?.toString()?.toRequestBody("application/json".toMediaType())
        builder.method(method, payload).header("Connection", "close")
        return adminHttp.newCall(builder.build()).execute().use {
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
    private fun parsePairing(pairing: JSONObject): PairingQr {
        val payload = pairing.getJSONObject("qr_payload")
        assertEquals(expectedPublicServerUrl, payload.getString("server"))
        val text = payload.toString()
        val qr = PairingQr.parse(text, true)
        assertEquals("https", qr.server.scheme)
        assertEquals(expectedPublicServerUrl.toHttpUrl().host, qr.server.host)
        assertEquals(443, qr.server.port)
        assertEquals("$expectedPublicServerUrl/", qr.server.toString())
        assertEquals(qr, PairingQr.parse(text, false))
        return qr
    }
    private fun capabilities(id: String): Set<String> {
        val array = http("GET", "/v1/devices/$id").getJSONArray("capabilities")
        return (0 until array.length()).map { array.getString(it) }.toSet()
    }
    private fun enqueue(id: String, op: String, args: JSONObject = JSONObject(), ttl: Int = 60, key: String? = null): JSONObject {
        val payload = JSONObject().put("op", op).put("args", args).put("ttl_seconds", ttl)
        key?.let { payload.put("idempotency_key", it) }
        return http("POST", "/v1/devices/$id/commands", payload)
    }
    private fun sha256(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
    private fun upload(id: String, name: String, mime: String, kind: String, bytes: ByteArray): JSONObject {
        val url = "$server/v1/devices/$id/files".toHttpUrl().newBuilder().addQueryParameter("name", name).addQueryParameter("mime", mime)
            .addQueryParameter("kind", kind).addQueryParameter("ttl_seconds", "300").build()
        val request = Request.Builder().url(url).header("Authorization", "Bearer $adminToken").header("X-Content-SHA256", sha256(bytes))
            .header("Idempotency-Key", UUID.randomUUID().toString()).header("Connection", "close").post(bytes.toRequestBody("application/octet-stream".toMediaType())).build()
        return adminHttp.newCall(request).execute().use {
            assertEquals("Upload must be queued", 200, it.code)
            JSONObject(it.body!!.string())
        }
    }
    private fun read(uri: Uri): ByteArray = context.contentResolver.openInputStream(uri)!!.use { it.readBytes() }
    private fun png(): ByteArray {
        val bitmap = Bitmap.createBitmap(3, 2, Bitmap.Config.ARGB_8888).apply { eraseColor(0xff336699.toInt()) }
        return ByteArrayOutputStream().also { bitmap.compress(Bitmap.CompressFormat.PNG, 100, it) }.toByteArray()
    }
    private fun deliver(id: String, name: String, mime: String, kind: String, bytes: ByteArray): JSONObject {
        val result = terminal(upload(id, name, mime, kind, bytes).getString("id"))
        assertEquals("files.receive", result.getString("op"))
        assertEquals("succeeded", result.getString("status"))
        val data = result.getJSONObject("data")
        assertEquals(setOf("uri", "name", "size", "sha256", "mime", "kind"), data.keys().asSequence().toSet())
        assertEquals(bytes.size.toLong(), data.getLong("size"))
        assertEquals(sha256(bytes), data.getString("sha256"))
        val uri = Uri.parse(data.getString("uri"))
        delivered += uri
        assertArrayEquals(bytes, read(uri))
        val folder = if (kind == "image") "Pictures/PhoneBridge/" else "Download/PhoneBridge/"
        context.contentResolver.query(uri, arrayOf(android.provider.MediaStore.MediaColumns.RELATIVE_PATH, android.provider.MediaStore.MediaColumns.IS_PENDING, android.provider.MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)!!.use {
            assertTrue(it.moveToFirst())
            assertEquals(folder, it.getString(0))
            assertEquals(0, it.getInt(1))
            assertEquals(data.getString("name"), it.getString(2))
        }
        return data
    }
    private fun terminal(id: String): JSONObject {
        var value = JSONObject()
        waitUntil { value = http("GET", "/v1/commands/$id"); value.getString("status") in setOf("succeeded", "failed", "expired") }
        return value
    }
    @Before fun setup() {
        val args = InstrumentationRegistry.getArguments()
        server = args.getString("serverUrl")?.trimEnd('/') ?: error("Automatic CI must supply serverUrl")
        expectedPublicServerUrl = args.getString("expectedPublicServerUrl") ?: error("Automatic CI must supply expectedPublicServerUrl")
        adminToken = args.getString("adminToken") ?: error("Automatic CI must supply adminToken")
        assertEquals("http://10.0.2.2:18940", server)
        assertTrue(expectedPublicServerUrl.matches(Regex("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.trycloudflare\\.com")))
        assertEquals("$expectedPublicServerUrl/", expectedPublicServerUrl.toHttpUrl().toString())
        assertTrue(adminToken.matches(Regex("[A-Za-z0-9_-]{64}")))
        BridgeService.stop(context, true)
        waitUntil(5000) { BridgeService.current == null }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        assertEquals("ok", http("GET", "/healthz", admin = false).getString("status"))
    }
    @After fun cleanup() {
        recoverySocket?.cancel()
        scenario?.close()
        BridgeService.stop(context, true)
        delivered.forEach { context.contentResolver.delete(it, null, null) }
        deviceId?.let { id ->
            adminHttp.newCall(Request.Builder().url("$server/v1/devices/$id").header("Authorization", "Bearer $adminToken").header("Connection", "close").delete().build()).execute().close()
        }
    }
    @Test fun realPairingKeystoreWebSocketPermissionsEventsReconnectExpiryAndRevocation() {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Android CI").put("ttl_seconds", 300))
        val qr = parsePairing(pairing)
        val value = BridgeHttp.claim(qr, "Android CI API ${Build.VERSION.SDK_INT}", false)
        assertEquals("$expectedPublicServerUrl/", value.server)
        assertEquals("wss://${qr.server.host}/v1/device/ws", value.wsUrl)
        deviceId = value.deviceId
        val id = value.deviceId
        val status = http("GET", "/v1/pairings/${pairing.getString("id")}")
        assertEquals("claimed", status.getString("status"))
        assertEquals(id, status.getString("device_id"))
        try { BridgeHttp.claim(qr, "Duplicate", false); fail("One-time claim must be refused") } catch (e: BridgeFailure) { assertEquals("PAIRING_FAILED", e.code) }
        val credentials = Credentials(context)
        credentials.save(value)
        assertEquals(value, credentials.load())
        val plainPrefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE).all.values.joinToString()
        assertFalse(plainPrefs.contains(value.token))
        assertFalse(plainPrefs.contains(qr.token))
        scenario = ActivityScenario.launch(MainActivity::class.java)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { online(id) }
        assertTrue(capabilities(id).containsAll(setOf("device.info", "files.receive")))
        if (BridgeAccessibility.current == null || !ScreenControl.enabled(context)) assertTrue(capabilities(id).none { it.startsWith("screen.") })
        assertTrue(http("GET", "/v1/devices/$id").getJSONObject("permissions").has("accessibility"))
        val command = enqueue(id, "device.info", key = "device-info-dedup")
        val duplicate = enqueue(id, "device.info", key = "device-info-dedup")
        assertEquals(command.getString("id"), duplicate.getString("id"))
        val result = terminal(command.getString("id"))
        assertEquals("succeeded", result.getString("status"))
        assertEquals(Build.MODEL, result.getJSONObject("data").getString("model"))
        assertEquals(Build.VERSION.SDK_INT, result.getJSONObject("data").getInt("sdk"))
        val prefix = "ci-${UUID.randomUUID().toString().take(8)}"
        val binary = ByteArray(300_000) { ((it * 31 + 7) and 255).toByte() }
        deliver(id, "$prefix-binary.bin", "application/octet-stream", "file", binary)
        deliver(id, "$prefix-empty.txt", "text/plain", "file", byteArrayOf())
        val image = png()
        deliver(id, "$prefix-image.png", "image/png", "image", image)
        val decoded = BitmapFactory.decodeByteArray(image, 0, image.size)
        assertEquals(3, decoded.width)
        val first = deliver(id, "$prefix-same.bin", "application/octet-stream", "file", "first".toByteArray())
        val second = deliver(id, "$prefix-same.bin", "application/octet-stream", "file", "second, different".toByteArray())
        assertNotEquals(first.getString("uri"), second.getString("uri"))
        assertNotEquals(first.getString("name"), second.getString("name"))
        assertArrayEquals("first".toByteArray(), read(Uri.parse(first.getString("uri"))))
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
        val qr = parsePairing(pairing)
        val credentials = BridgeHttp.claim(qr, "Result recovery API ${Build.VERSION.SDK_INT}", false)
        assertEquals("$expectedPublicServerUrl/", credentials.server)
        assertEquals("wss://${qr.server.host}/v1/device/ws", credentials.wsUrl)
        deviceId = credentials.deviceId
        Credentials(context).save(credentials)
        scenario = ActivityScenario.launch(MainActivity::class.java)
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
        waitUntil { online(credentials.deviceId) && "device.info" in capabilities(credentials.deviceId) }
        val queued = enqueue(credentials.deviceId, "device.info")
        val frame = incoming.poll(10, TimeUnit.SECONDS) ?: error("Real server did not deliver the recovery command")
        assertEquals("command", frame.getString("type"))
        assertEquals(queued.getString("id"), frame.getString("id"))
        assertEquals("device.info", frame.getString("op"))
        var expected: JSONObject? = null
        val unknownId = UUID.randomUUID().toString()
        DurableStore(context).use { store ->
            assertNull(store.begin(frame))
            if (completed) {
                val data = NativeHandlers(context).execute(frame.getString("op"), frame.getJSONObject("args")).put("recovery_marker", UUID.randomUUID().toString())
                expected = Results.ok(frame.getString("id"), data)
                store.complete(frame.getString("id"), expected!!)
            }
            assertNull(store.begin(JSONObject().put("type", "command").put("id", unknownId).put("op", "device.info").put("args", JSONObject()).put("expires_at", frame.getLong("expires_at"))))
        }
        assertEquals("sent", http("GET", "/v1/commands/${frame.getString("id")}").getString("status"))
        recoverySocket!!.cancel()
        recoverySocket = null
        waitUntil { !http("GET", "/v1/devices/${credentials.deviceId}").getBoolean("online") }
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { online(credentials.deviceId) }
        val result = terminal(frame.getString("id"))
        if (completed) {
            assertEquals("succeeded", result.getString("status"))
            assertEquals(DurableStore.canonical(expected!!.getJSONObject("data")), DurableStore.canonical(result.getJSONObject("data")))
        } else {
            assertEquals("failed", result.getString("status"))
            assertEquals("AMBIGUOUS_STATE", result.getJSONObject("error").getString("code"))
        }
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
    @Test fun completedResultLostBeforeSendIsReturnedOnlyOnRealServerReconnectReplay() = realServerRecovery(true)
    @Test fun recoveredReservationReportsAmbiguityOnRealServerReplayWithoutExecutionOrUnknownResultFlood() = realServerRecovery(false)
    @Test fun expiredServerGeneratedQrCannotBeClaimedOrParsed() {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Expiry CI").put("ttl_seconds", 1))
        val payload = pairing.getJSONObject("qr_payload")
        assertEquals(expectedPublicServerUrl, payload.getString("server"))
        val text = payload.toString()
        val expiresAt = payload.getLong("expires_at")
        assertEquals(pairing.getLong("expires_at"), expiresAt)
        val waitStarted = SystemClock.elapsedRealtime()
        android.util.Log.i("PairingExpiryCI", "serverIssuedAt=${expiresAt - 1} expiresAt=$expiresAt deviceNow=${System.currentTimeMillis() / 1000}")
        val qr = PairingQr.parse(text, false, expiresAt - 1)
        assertEquals("https", qr.server.scheme)
        assertEquals(expectedPublicServerUrl.toHttpUrl().host, qr.server.host)
        assertEquals(443, qr.server.port)
        assertEquals("$expectedPublicServerUrl/", qr.server.toString())
        for (allowEmulator in listOf(true, false)) {
            assertEquals(qr, PairingQr.parse(text, allowEmulator, expiresAt - 1))
            for (now in listOf(expiresAt, expiresAt + 1, expiresAt - 86401)) {
                try {
                    PairingQr.parse(text, allowEmulator, now)
                    fail("The genuine QR must reject expiry boundaries and TTLs exceeding one day")
                } catch (e: BridgeFailure) {
                    assertEquals("VALIDATION_ERROR", e.code)
                }
            }
        }
        var expired = JSONObject()
        waitUntil {
            expired = http("GET", "/v1/pairings/${pairing.getString("id")}")
            expired.getString("status") == "expired"
        }
        assertEquals("expired", expired.getString("status"))
        assertEquals(expiresAt, expired.getLong("expires_at"))
        assertTrue("An expired pairing must not issue a device", expired.isNull("device_id"))
        android.util.Log.i("PairingExpiryCI", "serverStatus=expired expiresAt=$expiresAt deviceNow=${System.currentTimeMillis() / 1000} elapsedMs=${SystemClock.elapsedRealtime() - waitStarted}")
        val claim = JSONObject().put("token", payload.getString("token")).put("name", "Expired").put("platform", "android")
        BridgeHttp.client.newCall(Request.Builder().url("$expectedPublicServerUrl/v1/pair/claim").post(claim.toString().toRequestBody("application/json".toMediaType())).build()).execute().use { response ->
            assertEquals("The real server must reject the expired credential", 401, response.code)
            assertEquals("PAIRING_INVALID", JSONObject(response.body!!.string()).getJSONObject("error").getString("code"))
        }
        val refused = http("GET", "/v1/pairings/${pairing.getString("id")}")
        assertEquals("expired", refused.getString("status"))
        assertTrue("A refused expired claim must not create a paired device", refused.isNull("device_id"))
        assertNull(Credentials(context).load())
        waitUntil { System.currentTimeMillis() / 1000 >= expiresAt }
        android.util.Log.i("PairingExpiryCI", "deviceExpired=true expiresAt=$expiresAt deviceNow=${System.currentTimeMillis() / 1000} elapsedMs=${SystemClock.elapsedRealtime() - waitStarted}")
        assertTrue("The device clock must reach expiry before default-clock parsing", System.currentTimeMillis() / 1000 >= expiresAt)
        for (allowEmulator in listOf(true, false)) {
            try { PairingQr.parse(text, allowEmulator); fail("Expired QR must fail") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
            try { BridgeHttp.claim(qr, "Expired", allowEmulator); fail("The Android client must refuse an expired QR") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
        }
    }
}
