package org.nous.phonebridge

import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Build
import android.os.Environment
import android.os.SystemClock
import android.provider.MediaStore
import android.provider.Settings
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestRule
import org.junit.runner.RunWith
import org.junit.runners.model.Statement
import java.io.ByteArrayOutputStream
import java.io.DataOutputStream
import java.io.File
import java.security.MessageDigest
import java.util.concurrent.TimeUnit
import java.util.zip.Adler32
import java.util.zip.CRC32

@RunWith(AndroidJUnit4::class)
class FileDeliveryInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val resolver = context.contentResolver
    private val directory get() = File(context.filesDir, "file-delivery")
    private val httpClient = BridgeHttp.client.newBuilder().callTimeout(12, TimeUnit.SECONDS).build()
    private var deadline = 0L
    private var deviceId: String? = null
    private var scenario: ActivityScenario<MainActivity>? = null
    private lateinit var nonce: String
    private lateinit var server: String
    private lateinit var token: String
    private val published = mutableListOf<Uri>()
    private val commandIds = mutableSetOf<String>()

    @get:Rule val cleanupRule = TestRule { base, _ ->
        object : Statement() {
            override fun evaluate() {
                var failure: Throwable? = null
                try { base.evaluate() } catch (error: Throwable) { failure = error }
                val actions: List<() -> Unit> = listOf(
                    { deviceId?.let { request("DELETE", "/v1/devices/$it", expected = 204) }; Unit },
                    { deviceId?.let { request("GET", "/v1/devices/$it", expected = 404) }; Unit },
                    { scenario?.close(); scenario = null },
                    { BridgeService.stop(context, true) },
                    {
                        val stopDeadline = SystemClock.elapsedRealtime() + 10000
                        while (BridgeService.current != null && SystemClock.elapsedRealtime() < stopDeadline) Thread.sleep(100)
                        assertNull("FD_CLEANUP", BridgeService.current)
                        assertNull("FD_CLEANUP", Credentials(context).load())
                    },
                    { published.distinct().forEach { resolver.delete(it, null, null) }; Unit },
                    { if (directory.exists()) assertTrue("FD_CLEANUP", directory.deleteRecursively()) },
                    { httpClient.connectionPool.evictAll() }
                )
                actions.forEach { action ->
                    try { action() } catch (error: Throwable) {
                        if (failure == null) failure = error else if (failure !== error) failure?.addSuppressed(error)
                    }
                }
                failure?.let { throw it }
            }
        }
    }

    private data class Delivery(val id: String, val name: String, val mime: String, val kind: String, val bytes: ByteArray)

    private fun sha256(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it.toInt() and 255) }

    private fun png(): ByteArray {
        val output = ByteArrayOutputStream()
        val writer = DataOutputStream(output)
        writer.write(byteArrayOf(137.toByte(), 80, 78, 71, 13, 10, 26, 10))
        fun chunk(type: String, bytes: ByteArray) {
            val kind = type.toByteArray(Charsets.US_ASCII)
            writer.writeInt(bytes.size)
            writer.write(kind)
            writer.write(bytes)
            val crc = CRC32().apply { update(kind); update(bytes) }
            writer.writeInt(crc.value.toInt())
        }
        val header = ByteArrayOutputStream().also { stream ->
            DataOutputStream(stream).apply { writeInt(1); writeInt(1); write(byteArrayOf(8, 6, 0, 0, 0)) }
        }.toByteArray()
        chunk("IHDR", header)
        val scanline = byteArrayOf(0, 12, 34, 56, 255.toByte())
        val compressed = ByteArrayOutputStream().also { stream ->
            val data = DataOutputStream(stream)
            data.write(byteArrayOf(120, 1, 1, 5, 0, 250.toByte(), 255.toByte()))
            data.write(scanline)
            data.writeInt(Adler32().apply { update(scanline) }.value.toInt())
        }.toByteArray()
        chunk("IDAT", compressed)
        chunk("IEND", byteArrayOf())
        return output.toByteArray()
    }

    private fun deliveries(): List<Delivery> {
        val prefix = "fd-${nonce.take(8)}"
        return listOf(
            Delivery("binary", "$prefix-binary.bin", "application/octet-stream", "file", ByteArray(1024 * 1024 + 37) { ((it * 73 + 19) and 255).toByte() }),
            Delivery("image_flag", "$prefix-image-flag.png", "image/png", "image", png()),
            Delivery("image_alias", "$prefix-image-alias.png", "image/png", "image", png()),
            Delivery("empty", "$prefix-empty.bin", "application/octet-stream", "file", byteArrayOf()),
            Delivery("same_a", "$prefix-shared.bin", "application/octet-stream", "file", "PhoneBridge same name alpha\n".toByteArray()),
            Delivery("same_b", "$prefix-shared.bin", "application/octet-stream", "file", "PhoneBridge same name beta different\n".toByteArray())
        )
    }

    private fun waitUntil(condition: () -> Boolean) {
        while (SystemClock.elapsedRealtime() < deadline) {
            offState()
            if (directory.exists() && File(directory, "abort.json").exists()) {
                val abort = read("abort.json")!!
                assertEquals("FD_RECEIPT", nonce, abort.getString("nonce"))
                assertEquals("FD_RECEIPT", deviceId, abort.getString("device_id"))
                fail("FD_HOST")
            }
            if (condition()) return
            Thread.sleep(150)
        }
        fail("FD_DEADLINE")
    }

    private fun offState() {
        assertNull("FD_ACCESSIBILITY", BridgeAccessibility.current)
        assertEquals("FD_ACCESSIBILITY", 0, Settings.Secure.getInt(resolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0))
        val enabled = Settings.Secure.getString(resolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES).orEmpty()
        assertFalse("FD_ACCESSIBILITY", enabled.split(':').any { it.startsWith(context.packageName + "/") })
        val debug = DebugAccess.snapshot()
        assertFalse("FD_DEBUG", debug.enabled)
        assertNull("FD_DEBUG", debug.debugSession)
    }

    private fun request(method: String, path: String, body: JSONObject? = null, expected: Int = 200): JSONObject {
        val request = Request.Builder().url(server + path).header("Authorization", "Bearer $token")
            .method(method, body?.toString()?.toRequestBody("application/json".toMediaType())).build()
        return httpClient.newCall(request).execute().use { response ->
            assertEquals("FD_HTTP", expected, response.code)
            val source = response.body?.source()
            assertFalse("FD_HTTP_BOUND", source?.request(65537) == true)
            val text = source?.readUtf8().orEmpty()
            if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }

    private fun write(name: String, value: JSONObject) {
        val bytes = value.toString().toByteArray()
        assertTrue("FD_HANDSHAKE_BOUND", bytes.size <= 8192)
        val pending = File(directory, "$name.pending")
        pending.writeBytes(bytes)
        assertTrue("FD_HANDSHAKE_WRITE", pending.renameTo(File(directory, name)))
    }

    private fun read(name: String): JSONObject? {
        val file = File(directory, name)
        if (!file.exists()) return null
        assertTrue("FD_HANDSHAKE_BOUND", file.length() in 1..8192)
        val bytes = file.inputStream().use { stream ->
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(1024)
            while (output.size() <= 8192) {
                val count = stream.read(buffer, 0, minOf(buffer.size, 8193 - output.size()))
                if (count < 0) break
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        }
        assertTrue("FD_HANDSHAKE_BOUND", bytes.size <= 8192)
        return JSONObject(String(bytes, Charsets.UTF_8))
    }

    private fun keys(value: JSONObject) = value.keys().asSequence().toSet()

    private fun bound(value: JSONObject, index: Int, case: Delivery) {
        assertEquals("FD_RECEIPT", 1, value.getInt("version"))
        assertEquals("FD_RECEIPT", nonce, value.getString("nonce"))
        assertEquals("FD_RECEIPT", deviceId, value.getString("device_id"))
        assertEquals("FD_RECEIPT", index, value.getInt("index"))
        assertEquals("FD_RECEIPT", case.id, value.getString("case_id"))
    }

    private fun inspect(uri: Uri, result: JSONObject, case: Delivery): JSONObject {
        offState()
        assertEquals("FD_URI", "content", uri.scheme)
        assertEquals("FD_URI", MediaStore.AUTHORITY, uri.authority)
        val segments = uri.pathSegments
        assertEquals("FD_URI", if (case.kind == "image") 4 else 3, segments.size)
        assertTrue("FD_URI", segments[0] in setOf("external", "external_primary"))
        assertEquals("FD_URI", if (case.kind == "image") "images" else "downloads", segments[1])
        if (case.kind == "image") assertEquals("FD_URI", "media", segments[2])
        assertTrue("FD_URI", segments.last().matches(Regex("[0-9]+")))
        val expectedPath = (if (case.kind == "image") Environment.DIRECTORY_PICTURES else Environment.DIRECTORY_DOWNLOADS) + "/PhoneBridge/"
        val projection = arrayOf(MediaStore.MediaColumns.DISPLAY_NAME, MediaStore.MediaColumns.SIZE, MediaStore.MediaColumns.MIME_TYPE,
            MediaStore.MediaColumns.RELATIVE_PATH, MediaStore.MediaColumns.IS_PENDING, MediaStore.MediaColumns.OWNER_PACKAGE_NAME)
        var actualName = ""
        var actualPath = ""
        resolver.query(uri, projection, null, null, null).use { cursor ->
            assertNotNull("FD_MEDIASTORE", cursor)
            val row = cursor!!
            assertEquals("FD_MEDIASTORE", 1, row.count)
            assertTrue("FD_MEDIASTORE", row.moveToFirst())
            actualName = row.getString(0)
            assertEquals("FD_NAME", result.getString("name"), actualName)
            assertEquals("FD_SIZE", case.bytes.size.toLong(), row.getLong(1))
            assertEquals("FD_MIME", case.mime, row.getString(2))
            actualPath = row.getString(3)
            assertEquals("FD_PATH", expectedPath, actualPath)
            assertEquals("FD_PENDING", 0, row.getInt(4))
            assertEquals("FD_OWNER", context.packageName, row.getString(5))
        }
        val bytes = resolver.openInputStream(uri)?.use { stream ->
            val output = ByteArrayOutputStream()
            val buffer = ByteArray(32768)
            while (true) {
                val count = stream.read(buffer)
                if (count < 0) break
                assertTrue("FD_BYTES_BOUND", output.size() + count <= case.bytes.size)
                output.write(buffer, 0, count)
            }
            output.toByteArray()
        } ?: error("FD_BYTES")
        assertArrayEquals("FD_BYTES", case.bytes, bytes)
        val digest = sha256(bytes)
        assertEquals("FD_SHA256", sha256(case.bytes), digest)
        assertEquals("FD_SHA256", result.getString("sha256"), digest)
        if (case.kind == "image") {
            val bitmap = BitmapFactory.decodeByteArray(bytes, 0, bytes.size)
            assertNotNull("FD_BITMAP", bitmap)
            assertEquals("FD_BITMAP", 1, bitmap!!.width)
            assertEquals("FD_BITMAP", 1, bitmap.height)
            bitmap.recycle()
        }
        offState()
        return JSONObject().put("uri", uri.toString()).put("name", actualName).put("size", bytes.size)
            .put("sha256", digest).put("mime", case.mime).put("kind", case.kind).put("relative_path", actualPath)
            .put("is_pending", 0).put("accessibility_enabled", false).put("debug_enabled", false)
    }

    @Test fun installedCliUploadsReachNativeMediaStoreWithoutAccessibilityOrDebug() {
        deadline = SystemClock.elapsedRealtime() + 420000
        val arguments = InstrumentationRegistry.getArguments()
        assertEquals("FD_CI", "true", arguments.getString("automaticFileDeliveryCi"))
        assertEquals("FD_CI", "1", arguments.getString("ciRunAttempt"))
        assertTrue("FD_CI", Build.HARDWARE in setOf("ranchu", "goldfish"))
        assertTrue("FD_CI", Build.VERSION.SDK_INT in setOf(31, 35))
        nonce = arguments.getString("fileDeliveryNonce") ?: error("FD_NONCE")
        assertTrue("FD_NONCE", nonce.matches(Regex("[0-9a-f]{32}")))
        server = arguments.getString("expectedPublicServerUrl") ?: error("FD_SERVER")
        assertTrue("FD_SERVER", server.matches(Regex("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.trycloudflare\\.com")))
        token = arguments.getString("adminToken") ?: error("FD_TOKEN")
        assertTrue("FD_TOKEN", token.matches(Regex("[A-Za-z0-9_-]{64}")))
        BridgeService.stop(context, true)
        waitUntil { BridgeService.current == null }
        assertFalse("FD_STALE", directory.exists())
        assertTrue("FD_HANDSHAKE_WRITE", directory.mkdirs())
        val pairing = request("POST", "/v1/pairings", JSONObject().put("name", "File delivery synthetic CI").put("ttl_seconds", 300))
        val payload = pairing.getJSONObject("qr_payload")
        assertEquals("FD_PAIRING", server, payload.getString("server"))
        val qr = PairingQr.parse(payload.toString(), false)
        val claimed = BridgeHttp.claim(qr, "File delivery automatic CI", false)
        deviceId = claimed.deviceId
        assertEquals("FD_PAIRING", "$server/", claimed.server)
        assertEquals("FD_WSS", "wss://${qr.server.host}/v1/device/ws", claimed.wsUrl)
        Credentials(context).save(claimed)
        assertEquals("FD_CREDENTIALS", claimed, Credentials(context).load())
        scenario = ActivityScenario.launch(MainActivity::class.java)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil { request("GET", "/v1/devices/${claimed.deviceId}").optBoolean("online") }
        scenario!!.close()
        scenario = null
        waitUntil { !MainActivity.resumed }
        write("device.json", JSONObject().put("version", 1).put("nonce", nonce).put("device_id", claimed.deviceId))
        val cases = deliveries()
        val results = mutableListOf<JSONObject>()
        cases.forEachIndexed { index, case ->
            var receipt: JSONObject? = null
            waitUntil { receipt = read("receipt-$index.json"); receipt != null }
            val value = receipt!!
            assertEquals("FD_RECEIPT", setOf("version", "nonce", "device_id", "index", "case_id", "command_id", "request", "data"), keys(value))
            bound(value, index, case)
            val commandId = value.getString("command_id")
            assertTrue("FD_COMMAND", commandId.matches(Regex("[0-9a-f-]{36}")))
            assertTrue("FD_COMMAND", commandIds.add(commandId))
            val sent = value.getJSONObject("request")
            assertEquals("FD_REQUEST", setOf("name", "mime", "kind", "size", "sha256"), keys(sent))
            assertEquals("FD_REQUEST", case.name, sent.getString("name"))
            assertEquals("FD_REQUEST", case.mime, sent.getString("mime"))
            assertEquals("FD_REQUEST", case.kind, sent.getString("kind"))
            assertEquals("FD_REQUEST", case.bytes.size.toLong(), sent.getLong("size"))
            assertEquals("FD_REQUEST", sha256(case.bytes), sent.getString("sha256"))
            val data = value.getJSONObject("data")
            assertEquals("FD_RESULT", setOf("uri", "name", "size", "sha256", "mime", "kind"), keys(data))
            assertEquals("FD_RESULT", case.bytes.size.toLong(), data.getLong("size"))
            assertEquals("FD_RESULT", sha256(case.bytes), data.getString("sha256"))
            assertEquals("FD_RESULT", case.mime, data.getString("mime"))
            assertEquals("FD_RESULT", case.kind, data.getString("kind"))
            val command = request("GET", "/v1/commands/$commandId")
            assertEquals("FD_COMMAND", commandId, command.getString("id"))
            assertEquals("FD_COMMAND", deviceId, command.getString("device_id"))
            assertEquals("FD_COMMAND", "files.receive", command.getString("op"))
            assertEquals("FD_COMMAND", "succeeded", command.getString("status"))
            val stored = command.getJSONObject("data")
            data.keys().forEach { key -> assertEquals("FD_COMMAND", data.get(key), stored.get(key)) }
            val uri = Uri.parse(data.getString("uri"))
            assertFalse("FD_OVERWRITE", uri in published)
            published += uri
            results += data
            val proof = inspect(uri, data, case).put("version", 1).put("nonce", nonce).put("device_id", deviceId)
                .put("index", index).put("case_id", case.id).put("command_id", commandId)
            write("readback-$index.json", proof)
        }
        cases.forEachIndexed { index, case -> inspect(published[index], results[index], case) }
        assertEquals("FD_OVERWRITE", cases.size, published.distinct().size)
        write("complete.json", JSONObject().put("version", 1).put("nonce", nonce).put("device_id", deviceId)
            .put("verified", cases.size).put("uris_distinct", published.distinct().size).put("case_ids", JSONArray(cases.map { it.id })))
        var ack: JSONObject? = null
        waitUntil { ack = read("host-complete.json"); ack != null }
        assertEquals("FD_COMPLETE", setOf("version", "nonce", "device_id", "verified"), keys(ack!!))
        assertEquals("FD_COMPLETE", 1, ack!!.getInt("version"))
        assertEquals("FD_COMPLETE", nonce, ack!!.getString("nonce"))
        assertEquals("FD_COMPLETE", deviceId, ack!!.getString("device_id"))
        assertEquals("FD_COMPLETE", cases.size, ack!!.getInt("verified"))
        offState()
    }
}
