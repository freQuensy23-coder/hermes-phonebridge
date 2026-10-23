package org.nous.phonebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.graphics.BitmapFactory
import android.net.Uri
import android.os.Environment
import android.provider.MediaStore
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.Request
import org.json.JSONObject
import java.io.IOException
import java.security.MessageDigest
import java.util.concurrent.TimeUnit

class FileReceiver(private val context: Context, private val credentials: DeviceCredentials) {
    fun receive(args: Arguments): JSONObject {
        args.only("transfer_id", "name", "mime", "kind", "size", "sha256")
        val transfer = args.string("transfer_id", 128)!!
        requireBridge(transfer.matches(IDENTIFIER), "Invalid transfer_id")
        val name = args.string("name", 256)!!
        requireBridge(validName(name), "Invalid file name")
        val mime = args.string("mime", 192)!!
        requireBridge(mime.matches(MIME), "Invalid mime")
        val kind = args.string("kind", 8)!!
        requireBridge(kind == "file" || kind == "image", "kind must be file or image")
        if (kind == "image") requireBridge(mime in IMAGE_TYPES, "Images must be PNG, JPEG, WebP or GIF")
        val size = args.long("size", 0, MAX_BYTES)
        val sha256 = args.string("sha256", 64)!!
        requireBridge(sha256.matches(SHA256), "Invalid sha256")
        ExecutionGuard.check()
        val resolver = context.contentResolver
        val collection = if (kind == "image") MediaStore.Images.Media.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
            else MediaStore.Downloads.getContentUri(MediaStore.VOLUME_EXTERNAL_PRIMARY)
        val folder = (if (kind == "image") Environment.DIRECTORY_PICTURES else Environment.DIRECTORY_DOWNLOADS) + "/PhoneBridge"
        val values = ContentValues().apply {
            put(MediaStore.MediaColumns.DISPLAY_NAME, name)
            put(MediaStore.MediaColumns.MIME_TYPE, mime)
            put(MediaStore.MediaColumns.RELATIVE_PATH, folder)
            put(MediaStore.MediaColumns.IS_PENDING, 1)
        }
        val uri = resolver.insert(collection, values) ?: failure("STORAGE_ERROR", "Android refused to create the file")
        var published = false
        try {
            val digest = MessageDigest.getInstance("SHA-256")
            var written = 0L
            val url = credentials.server.toHttpUrl().newBuilder().encodedPath("/").addPathSegments("v1/device/files").addPathSegment(transfer).build()
            val request = Request.Builder().url(url).header("Authorization", "Bearer ${credentials.token}").get().build()
            try {
                client.newCall(request).execute().use { response ->
                    when (response.code) {
                        200 -> Unit
                        401 -> failure("PERMISSION_REQUIRED", "Server refused this device's credentials for the download")
                        404, 409, 410 -> failure("NOT_FOUND", "The file is no longer available on the server (HTTP ${response.code})")
                        else -> failure("NETWORK_ERROR", "Download failed with HTTP ${response.code}")
                    }
                    val body = response.body ?: failure("NETWORK_ERROR", "Download returned no body")
                    val output = resolver.openOutputStream(uri, "w") ?: failure("STORAGE_ERROR", "Cannot open the new file for writing")
                    output.use { sink ->
                        body.byteStream().use { source ->
                            val buffer = ByteArray(65536)
                            while (true) {
                                val count = source.read(buffer)
                                if (count < 0) break
                                written += count
                                if (written > size) failure("INTEGRITY_ERROR", "Server sent more bytes than announced")
                                digest.update(buffer, 0, count)
                                sink.write(buffer, 0, count)
                            }
                        }
                    }
                }
            } catch (error: IOException) {
                failure("NETWORK_ERROR", "Download interrupted: ${error.javaClass.simpleName}")
            }
            val actual = digest.digest().joinToString("") { "%02x".format(it) }
            if (written != size || actual != sha256) failure("INTEGRITY_ERROR", "Downloaded bytes do not match the announced size and SHA-256")
            if (kind == "image") requireDecodable(uri)
            ExecutionGuard.check()
            resolver.update(uri, ContentValues().apply { put(MediaStore.MediaColumns.IS_PENDING, 0) }, null, null)
            published = true
            val saved = resolver.query(uri, arrayOf(MediaStore.MediaColumns.DISPLAY_NAME), null, null, null)?.use { cursor ->
                if (cursor.moveToFirst()) cursor.getString(0) else null
            } ?: name
            notifyReceived(uri, saved, mime)
            return JSONObject().put("uri", uri.toString()).put("name", saved).put("size", written).put("sha256", actual).put("mime", mime).put("kind", kind)
        } finally {
            if (!published) try { resolver.delete(uri, null, null) } catch (_: Exception) { }
        }
    }

    private fun requireDecodable(uri: Uri) {
        val options = BitmapFactory.Options().apply { inJustDecodeBounds = true }
        context.contentResolver.openInputStream(uri)?.use { BitmapFactory.decodeStream(it, null, options) }
        if (options.outWidth <= 0 || options.outHeight <= 0) failure("VALIDATION_ERROR", "Image bytes cannot be decoded")
    }

    private fun notifyReceived(uri: Uri, name: String, mime: String) {
        try {
            val manager = context.getSystemService(NotificationManager::class.java)
            manager.createNotificationChannel(NotificationChannel(CHANNEL, "Received files", NotificationManager.IMPORTANCE_DEFAULT))
            val view = Intent(Intent.ACTION_VIEW).setDataAndType(uri, mime).addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_ACTIVITY_NEW_TASK)
            val id = (uri.lastPathSegment?.toLongOrNull() ?: uri.hashCode().toLong()).toInt()
            val open = PendingIntent.getActivity(context, id, view, PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
            val notification = Notification.Builder(context, CHANNEL).setSmallIcon(android.R.drawable.stat_sys_download_done)
                .setContentTitle("File received").setContentText(name).setContentIntent(open).setAutoCancel(true).build()
            manager.notify(NOTIFICATION_BASE + (id and 0xFFFF), notification)
        } catch (_: Exception) { }
    }

    companion object {
        const val MAX_BYTES = 33554432L
        const val CHANNEL = "phonebridge.files"
        private const val NOTIFICATION_BASE = 200000
        private val client = BridgeHttp.client.newBuilder().connectionPool(okhttp3.ConnectionPool(1, 30, TimeUnit.SECONDS)).retryOnConnectionFailure(true).readTimeout(60, TimeUnit.SECONDS).callTimeout(10, TimeUnit.MINUTES).build()
        private val IDENTIFIER = Regex("[A-Za-z0-9_.:-]{1,128}")
        private val MIME = Regex("[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,63}/[A-Za-z0-9][A-Za-z0-9!#$&^_.+-]{0,126}")
        private val SHA256 = Regex("[0-9a-f]{64}")
        val IMAGE_TYPES = setOf("image/png", "image/jpeg", "image/webp", "image/gif")
        fun validName(name: String): Boolean =
            name.isNotEmpty() && name != "." && name != ".." && name.toByteArray(Charsets.UTF_8).size <= 180 &&
                name.none { it == '/' || it == '\\' || it.code < 32 || it.code == 127 || Character.isSurrogate(it) && !validSurrogates(name) }
        private fun validSurrogates(name: String): Boolean {
            var index = 0
            while (index < name.length) {
                val char = name[index]
                if (Character.isHighSurrogate(char)) {
                    if (index + 1 >= name.length || !Character.isLowSurrogate(name[index + 1])) return false
                    index += 2
                } else if (Character.isLowSurrogate(char)) return false else index++
            }
            return true
        }
    }
}
