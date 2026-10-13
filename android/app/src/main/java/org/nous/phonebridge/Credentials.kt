package org.nous.phonebridge

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.MediaType.Companion.toMediaType
import org.json.JSONObject
import java.security.KeyStore
import java.util.concurrent.TimeUnit
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

data class DeviceCredentials(val deviceId: String, val token: String, val server: String, val wsUrl: String)

class Credentials(context: Context) {
    private val prefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE)
    private val alias = "phonebridge.device.v1"
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(alias, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").apply {
            init(KeyGenParameterSpec.Builder(alias, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT).setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).setRandomizedEncryptionRequired(true).build())
        }.generateKey()
    }
    @Synchronized fun save(value: DeviceCredentials) {
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val plain = JSONObject().put("id", value.deviceId).put("token", value.token).put("server", value.server).put("ws", value.wsUrl).toString().toByteArray()
        val encrypted = cipher.doFinal(plain)
        check(prefs.edit().putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP)).putString("ciphertext", Base64.encodeToString(encrypted, Base64.NO_WRAP)).commit())
    }
    @Synchronized fun load(): DeviceCredentials? {
        val encrypted = prefs.getString("ciphertext", null) ?: return null
        return try {
            val iv = Base64.decode(prefs.getString("iv", null) ?: return null, Base64.NO_WRAP)
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, iv)) }
            val obj = JSONObject(String(cipher.doFinal(Base64.decode(encrypted, Base64.NO_WRAP))))
            DeviceCredentials(obj.getString("id"), obj.getString("token"), obj.getString("server"), obj.getString("ws"))
        } catch (_: Exception) {
            clear()
            null
        }
    }
    @Synchronized fun clear() {
        prefs.edit().clear().commit()
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        if (store.containsAlias(alias)) store.deleteEntry(alias)
    }
}

object BridgeHttp {
    val client = OkHttpClient.Builder().connectTimeout(15, TimeUnit.SECONDS).readTimeout(25, TimeUnit.SECONDS).writeTimeout(15, TimeUnit.SECONDS).callTimeout(30, TimeUnit.SECONDS).followRedirects(false).followSslRedirects(false).retryOnConnectionFailure(false).build()
    fun claim(qr: PairingQr, name: String, allowEmulator: Boolean): DeviceCredentials {
        requireBridge(name.isNotBlank() && name.length <= 80, "Device name must be 1..80 characters")
        requireBridge(qr.expiresAt > System.currentTimeMillis() / 1000, "Pairing QR expired")
        val payload = JSONObject().put("token", qr.token).put("name", name).put("platform", "android")
        val request = Request.Builder().url(qr.server.newBuilder().encodedPath("/v1/pair/claim").build()).post(payload.toString().toRequestBody("application/json".toMediaType())).build()
        client.newCall(request).execute().use { response ->
            if (!response.isSuccessful) failure("PAIRING_FAILED", "Pairing refused (${response.code}); create a fresh QR")
            val source = response.body?.source() ?: failure("PAIRING_FAILED", "Missing claim response")
            requireBridge(!source.request(32769), "Claim response too large")
            val obj = JSONObject(source.readUtf8())
            val args = Arguments(obj)
            val id = args.string("device_id", 128)!!
            val token = args.string("device_token", 512)!!
            requireBridge(token.matches(Regex("[A-Za-z0-9_-]{43}")), "Invalid device token")
            val ws = args.string("ws_url", 2048)!!
            requireBridge(ws.startsWith(if (qr.server.isHttps) "wss://" else "ws://"), "WS transport must match pairing transport")
            val parsed = ws.replaceFirst("wss://", "https://").replaceFirst("ws://", "http://").toHttpUrl()
            requireBridge(parsed.host == qr.server.host && parsed.port == qr.server.port && parsed.username.isEmpty() && parsed.password.isEmpty() && parsed.query == null && parsed.fragment == null && parsed.encodedPath == "/v1/device/ws", "WS endpoint must match confirmed server origin")
            PairingQr.validateServer(qr.server, allowEmulator)
            return DeviceCredentials(id, token, qr.server.toString(), ws)
        }
    }
}
