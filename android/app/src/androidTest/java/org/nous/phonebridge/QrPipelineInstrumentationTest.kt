package org.nous.phonebridge

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.os.Build
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import com.google.zxing.BarcodeFormat
import com.google.zxing.DecodeHintType
import com.google.zxing.RGBLuminanceSource
import com.journeyapps.barcodescanner.DefaultDecoderFactory
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class QrPipelineInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val adminHttp = BridgeHttp.client.newBuilder().connectionPool(okhttp3.ConnectionPool(0, 1, java.util.concurrent.TimeUnit.SECONDS)).build()
    private lateinit var server: String
    private lateinit var expectedPublicServerUrl: String
    private lateinit var adminToken: String
    private lateinit var fixtureUrl: String
    private var isolatedFixture = false
    private var pairingId: String? = null
    private var deviceId: String? = null
    private var scenario: ActivityScenario<MainActivity>? = null

    @Test fun realCliPngDecodesValidatesClaimsAndExecutesDeviceInfoOverProductionWebSocket() {
        val metadataUrl = fixtureUrl.removeSuffix("pairing.png") + "pairing.json"
        val metadata = JSONObject(fetch(metadataUrl, 8192).toString(Charsets.UTF_8))
        val id = metadata.getString("id")
        assertEquals("CLI pairing ID must be canonical", UUID.fromString(id).toString(), id)
        pairingId = id
        assertEquals("pending", metadata.getString("status"))
        assertFalse("CLI metadata must not expose the QR credential", metadata.has("qr_payload") || metadata.has("token"))

        val decoded = decodePng(fetch(fixtureUrl, 131072, "image/png"))
        assertEquals(expectedPublicServerUrl, JSONObject(decoded).getString("server"))
        val qr = PairingQr.parse(decoded, BuildConfig.DEBUG)
        val expectedOrigin = expectedPublicServerUrl.toHttpUrl()
        assertEquals("https", qr.server.scheme)
        assertEquals(expectedOrigin.host, qr.server.host)
        assertEquals(443, qr.server.port)
        assertEquals("$expectedPublicServerUrl/", qr.server.toString())
        assertEquals(metadata.getLong("expires_at"), qr.expiresAt)
        val now = System.currentTimeMillis() / 1000
        assertTrue("CLI QR must still be fresh when Android consumes it", qr.expiresAt > now)
        assertTrue("CI pairing must respect the server's maximum TTL", qr.expiresAt <= now + 900)
        assertEquals("Release validation must accept the genuine public HTTPS QR", qr, PairingQr.parse(decoded, false))
        for (allowEmulator in listOf(true, false)) {
            try {
                PairingQr.parse(decoded, allowEmulator, qr.expiresAt)
                fail("The decoded QR must be rejected at its expiry boundary")
            } catch (e: BridgeFailure) {
                assertEquals("VALIDATION_ERROR", e.code)
            }
        }

        val pending = http("GET", "/v1/pairings/$id")
        assertEquals("pending", pending.getString("status"))
        assertEquals(qr.expiresAt, pending.getLong("expires_at"))
        val value = BridgeHttp.claim(qr, "QR-CI API ${Build.VERSION.SDK_INT}", false)
        deviceId = value.deviceId
        assertEquals("$expectedPublicServerUrl/", value.server)
        assertEquals("wss://${expectedOrigin.host}/v1/device/ws", value.wsUrl)
        assertTrue("Claim must issue a distinct device credential", value.token != qr.token)
        val claimed = http("GET", "/v1/pairings/$id")
        assertEquals("claimed", claimed.getString("status"))
        assertEquals(value.deviceId, claimed.getString("device_id"))
        try {
            BridgeHttp.claim(qr, "QR-CI duplicate", false)
            fail("The real CLI QR must be one-use")
        } catch (e: BridgeFailure) {
            assertEquals("PAIRING_FAILED", e.code)
        }

        val credentials = Credentials(context)
        credentials.save(value)
        assertTrue("Android Keystore credentials must round-trip", credentials.load() == value)
        val connections = context.getSharedPreferences("connection", android.content.Context.MODE_PRIVATE).getLong("connections", 0)
        scenario = ActivityScenario.launch(MainActivity::class.java)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil {
            val device = http("GET", "/v1/devices/${value.deviceId}")
            val capabilities = device.getJSONArray("capabilities")
            device.optBoolean("online") && device.optString("version") == BuildConfig.VERSION_NAME &&
                (0 until capabilities.length()).any { capabilities.getString(it) == "device.info" }
        }
        assertNotNull("The production foreground service must own the transport", BridgeService.current)
        val prefs = context.getSharedPreferences("connection", android.content.Context.MODE_PRIVATE)
        assertTrue("Production WebSocket onOpen must have run", prefs.getLong("connections", 0) > connections)
        assertEquals("Connected", prefs.getString("status", ""))

        val command = http("POST", "/v1/devices/${value.deviceId}/commands", JSONObject()
            .put("op", "device.info")
            .put("args", JSONObject())
            .put("ttl_seconds", 60)
            .put("idempotency_key", "qr-ci-${UUID.randomUUID()}"))
        var result = JSONObject()
        waitUntil {
            result = http("GET", "/v1/commands/${command.getString("id")}")
            result.getString("status") in setOf("succeeded", "failed", "expired")
        }
        assertEquals(command.getString("id"), result.getString("id"))
        assertEquals(value.deviceId, result.getString("device_id"))
        assertEquals("device.info", result.getString("op"))
        assertEquals("succeeded", result.getString("status"))
        assertTrue("Successful native execution must not carry an error", result.isNull("error"))
        val data = result.getJSONObject("data")
        assertEquals(Build.MANUFACTURER, data.getString("manufacturer"))
        assertEquals(Build.MODEL, data.getString("model"))
        assertEquals(Build.VERSION.SDK_INT, data.getInt("sdk"))
        assertEquals(Build.VERSION.RELEASE, data.getString("android"))
        assertEquals(BuildConfig.VERSION_NAME, data.getString("version"))
    }

    @Before fun setup() {
        val args = InstrumentationRegistry.getArguments()
        assertEquals("Automatic CI must explicitly enable the isolated CLI PNG fixture", "true", args.getString("qrFixture"))
        server = args.getString("serverUrl") ?: error("Automatic CI must supply serverUrl")
        expectedPublicServerUrl = args.getString("expectedPublicServerUrl") ?: error("Automatic CI must supply expectedPublicServerUrl")
        adminToken = args.getString("adminToken") ?: error("Automatic CI must supply adminToken")
        fixtureUrl = args.getString("qrFixtureUrl") ?: error("Automatic CI must supply qrFixtureUrl")
        assertTrue("Only the debug APK may use the isolated emulator admin endpoint", BuildConfig.DEBUG)
        assertEquals("http://10.0.2.2:18940", server)
        assertTrue("CI must supply an exact public HTTPS quick-tunnel origin", expectedPublicServerUrl.matches(Regex("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.trycloudflare\\.com")))
        assertEquals("$expectedPublicServerUrl/", expectedPublicServerUrl.toHttpUrl().toString())
        assertTrue("CI must supply a random synthetic admin token", adminToken.matches(Regex("[A-Za-z0-9_-]{64}")))
        assertEquals("http://10.0.2.2:18941/pairing.png", fixtureUrl)
        assertEquals("ok", http("GET", "/healthz", admin = false).getString("status"))
        val publicHealth = JSONObject(fetch("$expectedPublicServerUrl/healthz", 8192).toString(Charsets.UTF_8))
        assertEquals("ok", publicHealth.getString("status"))
        assertEquals(BuildConfig.VERSION_NAME, publicHealth.getString("version"))
        isolatedFixture = true
        BridgeService.stop(context, true)
        waitUntil(5000) { BridgeService.current == null }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        assertNull(Credentials(context).load())
    }

    @After fun cleanup() {
        if (!isolatedFixture) return
        try {
            scenario?.close()
        } finally {
            BridgeService.stop(context, true)
            waitUntil(5000) { BridgeService.current == null }
            assertNull(Credentials(context).load())
            pairingId?.let { id ->
                http("DELETE", "/v1/pairings/$id", expected = 204)
                assertEquals("revoked", http("GET", "/v1/pairings/$id").getString("status"))
            }
            deviceId?.let { id ->
                http("DELETE", "/v1/devices/$id", expected = 204)
                http("GET", "/v1/devices/$id", expected = 404)
            }
        }
    }

    private fun fetch(url: String, maximum: Int, contentType: String? = null): ByteArray {
        return BridgeHttp.client.newCall(Request.Builder().url(url).get().build()).execute().use { response ->
            assertEquals("Fixture fetch must succeed without redirects", 200, response.code)
            val body = response.body ?: error("Missing CLI fixture body")
            contentType?.let { assertEquals(it, body.contentType()?.let { type -> "${type.type}/${type.subtype}" }) }
            assertTrue("Fixture exceeds its byte limit", body.contentLength() <= maximum.toLong())
            val source = body.source()
            assertFalse("Fixture exceeds its bounded streaming limit", source.request(maximum.toLong() + 1))
            source.readByteArray().also { assertTrue("CLI fixture must not be empty", it.isNotEmpty()) }
        }
    }

    private fun decodePng(bytes: ByteArray): String {
        val signature = byteArrayOf(0x89.toByte(), 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a)
        assertTrue("CLI fixture must be a PNG", bytes.size >= signature.size && bytes.copyOfRange(0, signature.size).contentEquals(signature))
        val bounds = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        BitmapFactory.decodeByteArray(bytes, 0, bytes.size, bounds)
        assertEquals("image/png", bounds.outMimeType)
        assertTrue("PNG dimensions must be bounded before bitmap allocation", bounds.outWidth in 1..1024 && bounds.outHeight in 1..1024)
        assertTrue("PNG pixel count must be bounded", bounds.outWidth.toLong() * bounds.outHeight <= 1_048_576)
        val options = BitmapFactory.Options().apply { inPreferredConfig = Bitmap.Config.ARGB_8888; inScaled = false }
        val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size, options) ?: error("Android could not decode the real CLI PNG")
        return try {
            assertEquals(bounds.outWidth, bitmap.width)
            assertEquals(bounds.outHeight, bitmap.height)
            val pixels = IntArray(bitmap.width * bitmap.height)
            bitmap.getPixels(pixels, 0, bitmap.width, 0, 0, bitmap.width, bitmap.height)
            val decoder = DefaultDecoderFactory(listOf(BarcodeFormat.QR_CODE)).createDecoder(mapOf<DecodeHintType, Any>(DecodeHintType.TRY_HARDER to true))
            val result = decoder.decode(RGBLuminanceSource(bitmap.width, bitmap.height, pixels))
                ?: error("Production JourneyApps decoder did not find a QR in the real CLI PNG")
            assertEquals(BarcodeFormat.QR_CODE, result.barcodeFormat)
            assertTrue("Decoded pairing text must be bounded", result.text.toByteArray(Charsets.UTF_8).size in 1..4096)
            result.text
        } finally {
            bitmap.recycle()
        }
    }

    private fun http(method: String, path: String, body: JSONObject? = null, admin: Boolean = true, expected: Int = 200): JSONObject {
        val builder = Request.Builder().url(server + path)
        if (admin) builder.header("Authorization", "Bearer $adminToken")
        builder.method(method, body?.toString()?.toRequestBody("application/json".toMediaType())).header("Connection", "close")
        return adminHttp.newCall(builder.build()).execute().use { response ->
            assertEquals("Unexpected real server status for $method $path", expected, response.code)
            val source = response.body?.source() ?: return@use JSONObject()
            assertFalse("Real server response exceeds the test bound", source.request(32769))
            val text = source.readUtf8()
            if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }

    private fun waitUntil(timeoutMs: Long = 30000, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + timeoutMs
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(250)
        }
        fail("Real QR pairing pipeline did not complete within its bounded timeout")
    }

    private fun shell(command: String) = instrumentation.uiAutomation.executeShellCommand(command).use { parcel ->
        android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { it.readText() }
    }
}
