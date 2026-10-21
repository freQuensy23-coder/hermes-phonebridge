package org.nous.phonebridge

import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrlOrNull
import org.json.JSONArray
import org.json.JSONObject
import java.util.Calendar

class BridgeFailure(val code: String, override val message: String) : RuntimeException(message)

fun failure(code: String, message: String): Nothing = throw BridgeFailure(code, message)
fun requireBridge(value: Boolean, message: String) {
    if (!value) failure("VALIDATION_ERROR", message)
}

class Arguments(val json: JSONObject) {
    fun string(key: String, maximum: Int = 256, required: Boolean = true): String? {
        if (!json.has(key) || json.isNull(key)) {
            if (required) failure("VALIDATION_ERROR", "$key is required")
            return null
        }
        val value = json.get(key)
        requireBridge(value is String && value.length <= maximum && !value.contains('\u0000'), "Invalid $key")
        return value as String
    }
    fun int(key: String, minimum: Int, maximum: Int, default: Int? = null): Int {
        if (!json.has(key)) return default ?: failure("VALIDATION_ERROR", "$key is required")
        val value = json.get(key)
        requireBridge(value is Int || value is Long, "Invalid $key")
        val number = (value as Number).toLong()
        requireBridge(number >= minimum && number <= maximum, "Invalid $key")
        return number.toInt()
    }
    fun long(key: String, minimum: Long, maximum: Long): Long {
        val value = json.opt(key)
        requireBridge(value is Int || value is Long, "Invalid $key")
        val number = (value as Number).toLong()
        requireBridge(number >= minimum && number <= maximum, "Invalid $key")
        return number
    }
    fun bool(key: String): Boolean {
        requireBridge(json.opt(key) is Boolean, "Invalid $key")
        return json.getBoolean(key)
    }
    fun only(vararg keys: String) {
        requireBridge(json.keys().asSequence().all { it in keys }, "Unknown argument")
    }
    fun limit() = int("limit", 1, 200, 50)
}

data class PairingQr(val server: HttpUrl, val token: String, val expiresAt: Long) {
    companion object {
        fun parse(text: String, allowEmulator: Boolean, now: Long = System.currentTimeMillis() / 1000): PairingQr {
            requireBridge(text.toByteArray().size <= 4096, "QR is too large")
            val obj = try { JSONObject(text) } catch (_: Exception) { failure("VALIDATION_ERROR", "Invalid QR JSON") }
            val args = Arguments(obj)
            args.only("v", "server", "token", "expires_at")
            args.int("v", 1, 1)
            val server = args.string("server", 2048)!!.toHttpUrlOrNull() ?: failure("VALIDATION_ERROR", "Invalid server URL")
            validateServer(server, allowEmulator)
            val token = args.string("token", 512)!!
            requireBridge(token.matches(Regex("[A-Za-z0-9_-]{43}")), "Invalid pairing token")
            val expires = args.long("expires_at", 1, Long.MAX_VALUE)
            requireBridge(expires > now && expires <= now + 86400, "QR expired or expiry exceeds one day")
            return PairingQr(server, token, expires)
        }
        fun validateServer(url: HttpUrl, allowEmulator: Boolean) {
            requireBridge(url.username.isEmpty() && url.password.isEmpty() && url.query == null && url.fragment == null, "Credentials, query and fragments are forbidden")
            requireBridge(url.encodedPath == "/", "Server must be an origin URL")
            requireBridge(url.host.isNotBlank() && (url.isHttps || (allowEmulator && url.scheme == "http" && url.host == "10.0.2.2")), "HTTPS required; debug HTTP is limited to emulator host 10.0.2.2")
        }
    }
}

fun calendarDays(days: JSONArray): IntArray {
    requireBridge(days.length() in 1..7, "days must contain 1..7 values")
    val result = (0 until days.length()).map { index ->
        val day = Arguments(JSONObject().put("day", days.get(index))).int("day", 1, 7)
        if (day == 7) Calendar.SUNDAY else day + 1
    }
    requireBridge(result.distinct().size == result.size, "Duplicate days")
    return result.toIntArray()
}

object PayloadBounds {
    const val SCREENSHOT_RESULT_BYTES = 384 * 1024
    const val SCREENSHOT_JPEG_BYTES = 240 * 1024

    fun validate(value: Any?, maxBytes: Int = 32768) = validateBounded(value, maxBytes, false)

    fun validateResult(frame: JSONObject, op: String? = null) {
        val screenshot = op == "screen.screenshot" && frame.optBoolean("ok", false)
        if (screenshot) {
            val data = frame.getJSONObject("data")
            requireBridge(data.optString("mime") == "image/jpeg", "Invalid screenshot MIME")
            Arguments(data).int("width", 1, 1280)
            Arguments(data).int("height", 1, 1280)
            val bounds = data.optJSONArray("screen_bounds") ?: failure("VALIDATION_ERROR", "Missing native screen_bounds")
            requireBridge(bounds.length() == 4, "Invalid native screen_bounds")
            val coordinates = (0..3).map { Arguments(JSONObject().put("coordinate", bounds.get(it))).int("coordinate", -100000, 100000) }
            requireBridge(coordinates[2] > coordinates[0] && coordinates[3] > coordinates[1], "Empty native screen_bounds")
            val encoded = Arguments(data).string("base64", 327680)!!
            requireBridge(encoded.isNotEmpty() && encoded.length % 4 == 0 && encoded.matches(Regex("[A-Za-z0-9+/]*={0,2}")), "Invalid screenshot base64")
            val padding = encoded.takeLast(2).count { it == '=' }
            requireBridge(encoded.length / 4 * 3 - padding <= SCREENSHOT_JPEG_BYTES, "Screenshot JPEG exceeds bound")
        } else if (frame.optBoolean("ok", false)) {
            validate(frame.getJSONObject("data"))
        }
        validateBounded(frame, if (screenshot) SCREENSHOT_RESULT_BYTES else 120000, screenshot)
    }

    private fun validateBounded(value: Any?, maxBytes: Int, screenshot: Boolean) {
        var remaining = 2048
        fun visit(item: Any?, depth: Int, path: List<String>) {
            requireBridge(depth <= 12 && --remaining >= 0, "JSON nesting or item bound exceeded")
            when (item) {
                is JSONObject -> {
                    requireBridge(item.length() <= 128, "JSON object bound exceeded")
                    item.keys().forEach { key -> requireBridge(key.length <= 128, "JSON key bound exceeded"); visit(item.get(key), depth + 1, path + key) }
                }
                is JSONArray -> {
                    requireBridge(item.length() <= 512, "JSON array bound exceeded")
                    for (index in 0 until item.length()) visit(item.get(index), depth + 1, path + index.toString())
                }
                is String -> requireBridge(item.length <= if (screenshot && path == listOf("data", "base64")) 327680 else 16384, "JSON string bound exceeded")
                is Number -> requireBridge(item.toDouble().isFinite(), "Non-finite JSON number")
            }
        }
        visit(value, 0, emptyList())
        requireBridge(value.toString().toByteArray().size <= maxBytes, "JSON byte bound exceeded")
    }
}

object Results {
    fun ok(id: String, data: JSONObject) = JSONObject().put("type", "result").put("id", id).put("ok", true).put("data", data).put("error", JSONObject.NULL)
    fun error(id: String, code: String, message: String) = JSONObject().put("type", "result").put("id", id).put("ok", false).put("data", JSONObject.NULL).put("error", JSONObject().put("code", code).put("message", message))
}
