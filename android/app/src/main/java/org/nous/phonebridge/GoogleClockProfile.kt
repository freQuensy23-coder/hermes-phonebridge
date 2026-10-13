package org.nous.phonebridge

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import java.io.File
import java.security.MessageDigest

internal object GoogleClockProfile {
    const val PACKAGE = "com.google.android.deskclock"
    const val ACTIVITY = "com.android.deskclock.DeskClock"
    const val VERSION = "8.2 (805685915)"
    const val CODE = 76008260L
    const val APK_SHA256 = "cf71cbc0728bb44e37d5ad19772b6ae9b39aae07b46945e7cde3a930cbccd383"
    const val SIGNER_SHA256 = "f0fd6c5b410f25cb25c3b53346c8972fae30f8ee7411df910480ad6b2d60db83"
    const val APK_BYTES = 13046103L
    val operations = setOf("clock.alarms.list", "clock.alarms.enable", "clock.alarms.disable", "clock.alarms.delete", "clock.timers.list", "clock.timers.cancel", "clock.timers.pause", "clock.timers.resume")

    fun accepts(version: String, code: Long, digest: String, signers: Set<String>, split: Boolean, sdk: Int, language: String): Boolean =
        version == VERSION && code == CODE && digest == APK_SHA256 && signers == setOf(SIGNER_SHA256) && !split && sdk in setOf(31, 35) && language == "en"

    fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    fun installed(context: Context): Boolean = try {
        val pm = context.packageManager
        val info = pm.getPackageInfo(PACKAGE, PackageManager.GET_SIGNING_CERTIFICATES)
        val app = pm.getApplicationInfo(PACKAGE, 0)
        val apk = File(app.sourceDir)
        val signatures = info.signingInfo?.apkContentsSigners ?: emptyArray()
        val language = pm.getResourcesForApplication(app).configuration.locales[0]?.language ?: ""
        if (info.versionName != VERSION || info.longVersionCode != CODE || apk.length() != APK_BYTES || signatures.size != 1 || !app.splitSourceDirs.isNullOrEmpty() || android.provider.Settings.System.getString(context.contentResolver, android.provider.Settings.System.TIME_12_24) != "24") false
        else {
            val hash = MessageDigest.getInstance("SHA-256")
            apk.inputStream().use { stream ->
                val buffer = ByteArray(65536)
                var read = stream.read(buffer)
                var total = 0L
                while (read >= 0) {
                    total += read
                    if (total > APK_BYTES) failure("UNSUPPORTED", "Google Clock artifact exceeds profile bound")
                    hash.update(buffer, 0, read)
                    read = stream.read(buffer)
                }
                if (total != APK_BYTES) failure("UNSUPPORTED", "Google Clock artifact changed")
            }
            accepts(info.versionName ?: "", info.longVersionCode, hash.digest().joinToString("") { "%02x".format(it) }, signatures.map { digest(it.toByteArray()) }.toSet(), false, Build.VERSION.SDK_INT, language) && context.resources.configuration.locales[0]?.language == "en"
        }
    } catch (_: Exception) { false }

    fun requireInstalled(context: Context) {
        if (!installed(context)) failure("UNSUPPORTED", "Requires the exact signed Google Clock 8.2 artifact, English locale and API 31 or 35")
    }
}

internal data class GoogleClockBounds(val left: Int, val top: Int, val right: Int, val bottom: Int) {
    val nonempty get() = left < right && top < bottom
}

internal object GoogleClockWindows {
    fun system(sdk: Int, type: Int, active: Boolean, focused: Boolean, pkg: String?, bounds: GoogleClockBounds, navigationBackgrounds: List<GoogleClockBounds>): Boolean =
        type == 3 && !active && !focused && (pkg == "com.android.systemui" || (sdk == 35 && pkg == "com.android.launcher3" && bounds.nonempty && navigationBackgrounds.count { it == bounds } == 1))
}

internal class GoogleClockReadback(private val expected: List<GoogleClockEntry>, private val window: Int) {
    private var since: Long? = null
    fun observe(values: List<GoogleClockEntry>, currentWindow: Int, now: Long): Boolean {
        if (currentWindow != window) failure("UNSUPPORTED", "Clock window changed during readback")
        if (!GoogleClockCollection.same(values, expected)) {
            since = null
            return false
        }
        val first = since ?: now.also { since = it }
        return now - first >= 200
    }

    companion object {
        const val TIMEOUT_MS = 10000L
        val RECREATE_FLAGS = android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK
        fun requireRecreated(previousWindow: Int, currentWindow: Int, previousPresent: Boolean) {
            if (currentWindow == previousWindow || previousPresent) failure("UNSUPPORTED", "Clock retained its old collection window; model rebind is unproven")
        }
    }
}

internal data class GoogleClockEntry(val label: String, val time: String, val state: String, val schedule: String) {
    val key: Pair<String, String> get() = label to time
    fun withState(value: String) = copy(state = value)
}

internal object GoogleClockIdentity {
    fun alarm(time: String, clockDescription: String, label: String, cardDescription: String, switchDescription: String) {
        val cardLabel = label.ifEmpty { "Alarm" }
        if (!time.matches(Regex("(?:[01][0-9]|2[0-3]):[0-5][0-9]")) || clockDescription != time || switchDescription != "$time alarm" || cardDescription !in setOf("$time $cardLabel", " $cardLabel")) failure("UNSUPPORTED", "Clock alarm identity is not independently corroborated")
    }
}

internal enum class GoogleClockTimerSurface(val nodeClass: String, val action: String, val reset: String, val deletion: String, val addition: String) {
    CLASSIC("android.support.v7.widget.RecyclerView", "play_pause", "secondary_button", "tertiary_button", "primary_button"),
    GASTOWN("android.widget.GridView", "play_pause_button", "reset_button", "delete_button", "add_one_min_button");

    fun state(label: String, actionDescription: String, resetDescription: String, deleteDescription: String): String {
        val suffix = if (this == GASTOWN) " $label" else ""
        if (resetDescription != "Reset$suffix" || deleteDescription != "Delete $label") failure("UNSUPPORTED", "Unknown Clock timer controls")
        return when (actionDescription) {
            "Pause$suffix" -> "running"
            "Start$suffix" -> "paused"
            else -> failure("UNSUPPORTED", "Expired, missed, reset or unknown Clock timer state")
        }
    }

    companion object {
        fun fromClass(value: String): GoogleClockTimerSurface = values().singleOrNull { it.nodeClass == value } ?: failure("UNSUPPORTED", "Clock timer collection surface is unknown")
    }
}

internal object GoogleClockCollection {
    fun count(rows: Int, columns: Int, alarm: Boolean): Int {
        val count = rows * columns
        if (rows !in 0..8 || columns !in 0..(if (alarm) 1 else 2) || count !in 0..8 || (count == 0 && (rows != 0 || columns != 0))) failure("UNSUPPORTED", "Unknown Clock collection rectangle")
        return count
    }

    fun requireComplete(entries: List<GoogleClockEntry>, count: Int, indexes: List<Int>) {
        if (count !in 0..8 || entries.size != count || indexes.sorted() != (0 until count).toList()) failure("UNSUPPORTED", "Complete bounded Clock collection is not exposed")
        if (entries.map { it.key }.distinct().size != entries.size) failure("UNSUPPORTED", "Duplicate current Clock identities")
    }

    fun select(entries: List<GoogleClockEntry>, label: String, time: String): GoogleClockEntry =
        entries.singleOrNull { it.key == (label to time) } ?: failure("NOT_FOUND", "Exact current Clock selection not found")

    fun expected(entries: List<GoogleClockEntry>, target: GoogleClockEntry, state: String?): List<GoogleClockEntry> =
        entries.mapNotNull { if (it.key != target.key) it else state?.let { value -> it.withState(value) } }

    fun same(left: List<GoogleClockEntry>, right: List<GoogleClockEntry>): Boolean = left.size == right.size && left.toSet() == right.toSet()
}
