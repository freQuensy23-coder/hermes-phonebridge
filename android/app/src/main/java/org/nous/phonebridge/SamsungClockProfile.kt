package org.nous.phonebridge

import android.content.Context
import android.content.pm.PackageManager
import android.os.Build
import android.provider.Settings
import android.view.View
import java.io.File
import java.security.MessageDigest

internal object SamsungClockProfile {
    const val PACKAGE = "com.sec.android.app.clockpackage"
    const val VERSION = "12.5.07.13"
    const val CODE = 1250713100L
    const val APK_SHA256 = "203f4393ba7761ae955ac02241674b5f4270d9d3f79e16ab7721cc9e2a400ee3"
    const val SIGNER_SHA256 = "34df0e7a9f1cf1892e45c056b4973cd81ccf148a4050d11aea4ac5a65f900a42"
    const val APK_BYTES = 35921595L
    val operations = setOf("clock.alarms.list", "clock.alarms.enable", "clock.alarms.disable", "clock.alarms.delete", "clock.timers.list", "clock.timers.pause", "clock.timers.resume", "clock.timers.cancel")

    fun accepts(version: String, code: Long, apk: String, signers: Set<String>, split: Boolean, sdk: Int, language: String, time24: Boolean, manufacturer: String): Boolean =
        version == VERSION && code == CODE && apk == APK_SHA256 && signers == setOf(SIGNER_SHA256) && !split && sdk == 36 && language == "en" && time24 && manufacturer.equals("samsung", true)

    fun digest(bytes: ByteArray): String = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }

    data class Binding(val version: String, val code: Long, val path: String, val modified: Long, val signers: Set<String>)

    fun binding(context: Context): Binding {
        ExecutionGuard.check()
        val pm = context.packageManager
        val info = try { pm.getPackageInfo(PACKAGE, PackageManager.GET_SIGNING_CERTIFICATES) } catch (_: PackageManager.NameNotFoundException) { failure("UNSUPPORTED", "Samsung Clock package is missing") }
        val app = pm.getApplicationInfo(PACKAGE, 0)
        val signers = info.signingInfo?.apkContentsSigners?.map { digest(it.toByteArray()) }?.toSet() ?: emptySet()
        val resources = pm.getResourcesForApplication(app)
        val language = resources.configuration.locales[0]?.language ?: ""
        val file = File(app.sourceDir)
        val time24 = Settings.System.getString(context.contentResolver, Settings.System.TIME_12_24) == "24"
        if (!accepts(info.versionName ?: "", info.longVersionCode, APK_SHA256, signers, !app.splitSourceDirs.isNullOrEmpty(), Build.VERSION.SDK_INT, language, time24, Build.MANUFACTURER) || file.length() != APK_BYTES || context.resources.configuration.locales[0]?.language != "en" || resources.configuration.layoutDirection != View.LAYOUT_DIRECTION_LTR) failure("UNSUPPORTED", "Requires source-bound Samsung Clock $VERSION/$CODE, sole current Samsung signer, unsplit APK, Samsung API36, English LTR and explicit 24-hour format; installed=${info.versionName}/${info.longVersionCode}")
        return Binding(info.versionName ?: "", info.longVersionCode, app.sourceDir, file.lastModified(), signers)
    }

    fun requireInstalled(context: Context): Binding {
        val before = binding(context)
        val hash = MessageDigest.getInstance("SHA-256")
        var total = 0L
        File(before.path).inputStream().use { stream ->
            val buffer = ByteArray(65536)
            while (true) {
                ExecutionGuard.check()
                val count = stream.read(buffer)
                if (count < 0) break
                total += count
                if (total > APK_BYTES) failure("UNSUPPORTED", "Samsung Clock APK exceeds source bound")
                hash.update(buffer, 0, count)
            }
        }
        if (total != APK_BYTES || hash.digest().joinToString("") { "%02x".format(it) } != APK_SHA256 || binding(context) != before) failure("UNSUPPORTED", "Samsung Clock installed bytes differ from inspected vendor APK")
        return before
    }

    fun installed(context: Context): Boolean = try { requireInstalled(context); true } catch (_: Exception) { false }
}

internal data class SamsungClockEntry(val label: String, val time: String, val state: String, val schedule: String = "") {
    val key get() = label to time
    val menuHeader get() = "$time ${label.ifEmpty { "Alarm" }}"
}

internal object SamsungClockPolicy {
    fun requireSchema(value: Boolean, message: String) {
        if (!value) failure("UNSUPPORTED", message)
    }

    fun text(value: String): String {
        requireSchema(value.isNotEmpty() && value.length <= 256 && value.none { it.isSurrogate() || it.isISOControl() || it in '\u202a'..'\u202e' || it in '\u2066'..'\u2069' || it == '\u2026' } && !value.contains("..."), "Samsung identity is empty, clipped, directional or incomplete")
        return value
    }

    fun alarmSchedule(label: String, spokenTime: String, switchDescription: String, itemDescription: String): String {
        val prefix = "$label, $spokenTime, "
        val body = itemDescription.removeSuffix(", Double tap to edit.")
        requireSchema(itemDescription.endsWith(", Double tap to edit.") && body.startsWith(prefix) && body.endsWith(", ") && body.dropLast(2) == switchDescription, "Samsung complete name/time/schedule is not source-corroborated in English")
        return text(body.substring(prefix.length))
    }

    fun popupTexts(values: List<String>, header: String) {
        requireSchema(values.size == 2 && values.count { it == header } == 1 && values.count { it == "Delete" } == 1, "Samsung popup full header/Delete schema unknown; Move and arbitrary dialogs are excluded")
    }

    fun timerState(action: String): String = when (action) {
        "Pause" -> "running"
        "Resume" -> "paused"
        else -> failure("UNSUPPORTED", "Samsung timer is expired, restart-only or unknown")
    }

    fun remaining(value: String): Long {
        requireSchema(value.matches(Regex("[0-9]{2}:[0-5][0-9]:[0-5][0-9]")), "Unknown Samsung remaining-time format")
        val fields = value.split(':').map { it.toLong() }
        val seconds = fields[0] * 3600 + fields[1] * 60 + fields[2]
        requireSchema(seconds in 10..86400, "Samsung timer is too close to expiry or outside profile safety margin")
        return seconds
    }

    fun count(rows: Int, columns: Int): Int {
        requireSchema(rows in 0..8 && (columns == 1 || (rows == 0 && columns == 0)), "Samsung complete single-column collection metadata unavailable")
        return rows
    }

    fun complete(entries: List<SamsungClockEntry>, count: Int, indexes: List<Int>) {
        requireSchema(count in 0..8 && entries.size == count && indexes.sorted() == (0 until count).toList(), "Samsung collection is incomplete, clipped, grouped or contains unknown items")
        requireSchema(entries.map { it.key }.distinct().size == entries.size, "Ambiguous Samsung current label/time identities")
        requireSchema(entries.map { it.menuHeader }.distinct().size == entries.size, "Ambiguous Samsung alarm menu headers")
    }

    fun select(entries: List<SamsungClockEntry>, label: String, time: String, state: String?): SamsungClockEntry {
        val matches = entries.filter { it.key == (label to time) }
        if (matches.isEmpty()) failure("NOT_FOUND", "Exact current Samsung Clock label/time not found")
        requireSchema(matches.size == 1, "Ambiguous Samsung current selection")
        return matches.single().also { requireSchema(state == null || state == it.state, "Samsung selected state changed; request a fresh list") }
    }

    fun expected(entries: List<SamsungClockEntry>, target: SamsungClockEntry, state: String?): List<SamsungClockEntry> =
        entries.mapNotNull { if (it.key != target.key) it else state?.let { value -> it.copy(state = value) } }

    fun same(left: List<SamsungClockEntry>, right: List<SamsungClockEntry>): Boolean =
        left.size == right.size && left.groupingBy { it }.eachCount() == right.groupingBy { it }.eachCount()
}
