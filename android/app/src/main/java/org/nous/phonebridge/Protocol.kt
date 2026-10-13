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
    fun validate(value: Any?, maxBytes: Int = 32768) {
        var remaining = 2048
        fun visit(item: Any?, depth: Int) {
            requireBridge(depth <= 12 && --remaining >= 0, "JSON nesting or item bound exceeded")
            when (item) {
                is JSONObject -> {
                    requireBridge(item.length() <= 128, "JSON object bound exceeded")
                    item.keys().forEach { key -> requireBridge(key.length <= 128, "JSON key bound exceeded"); visit(item.get(key), depth + 1) }
                }
                is JSONArray -> {
                    requireBridge(item.length() <= 512, "JSON array bound exceeded")
                    for (index in 0 until item.length()) visit(item.get(index), depth + 1)
                }
                is String -> requireBridge(item.length <= 16384, "JSON string bound exceeded")
                is Number -> requireBridge(item.toDouble().isFinite(), "Non-finite JSON number")
            }
        }
        visit(value, 0)
        requireBridge(value.toString().toByteArray().size <= maxBytes, "JSON byte bound exceeded")
    }
}

object Results {
    fun ok(id: String, data: JSONObject) = JSONObject().put("type", "result").put("id", id).put("ok", true).put("data", data).put("error", JSONObject.NULL)
    fun error(id: String, code: String, message: String) = JSONObject().put("type", "result").put("id", id).put("ok", false).put("data", JSONObject.NULL).put("error", JSONObject().put("code", code).put("message", message))
}
