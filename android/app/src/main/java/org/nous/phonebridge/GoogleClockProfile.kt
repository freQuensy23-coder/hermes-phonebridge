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

internal object GoogleClockAlarmDetail {
    val weekdays = listOf("Sunday", "Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday")
    fun requireSurface(resources: List<String>) {
        val required = listOf("clock", "alarm_label", "repeat_days", "save_button", "delete_button")
        val allowed = required + (0..6).map { "day_button_$it" } + listOf("action_bar_root", "container", "coordinator", "touch_outside", "design_bottom_sheet", "drag_handle", "clock_container", "clock_edit_button", "schedule_alarm_row", "schedule_alarm_details", "schedule_alarm_detail_1", "schedule_alarm_detail_2", "schedule_alarm_action", "settings_group", "pause_dismiss_setting_group", "pause_alarm_row", "pause_alarm_icon", "pause_alarm_details", "pause_alarm_detail_1", "pause_alarm_detail_2", "pause_alarm_action", "pause_alarm_add_icon", "alarm_setting_group", "label_setting", "label_setting_title", "ringtone_setting", "ringtone_setting_title", "alarm_ringtone", "vibrate", "integration_setting_group", "assistant_routine_container", "assistant_routine_setting_title", "assistant_routine_icon")
        if (required.any { resource -> resources.count { it == resource } != 1 } || resources.any { it !in allowed } || resources.distinct().size != resources.size) failure("UNSUPPORTED", "Clock alarm detail surface is incomplete, unknown or covered by a promotion")
    }
    fun schedule(days: List<Pair<String, Boolean>>): String {
        if (days.size != 7 || days.map { it.first }.toSet() != weekdays.toSet()) failure("UNSUPPORTED", "Clock detail recurrence is incomplete or duplicated")
        val selected = days.filter { it.second }.map { it.first }.toSet()
        return if (selected.size == 7) "every day" else (weekdays.drop(1) + weekdays.first()).filter { it in selected }.joinToString(", ")
    }
    fun requireIdentity(target: GoogleClockEntry, label: String, time: String, description: String, days: List<Pair<String, Boolean>>) {
        val recurrence = schedule(days)
        val expectedDays = target.schedule.split(", ").filter { it.isNotEmpty() }
        val sameSchedule = recurrence == target.schedule || (target.schedule.isNotEmpty() && target.schedule != "every day" && expectedDays.joinToString(", ") == target.schedule && expectedDays.distinct().size == expectedDays.size && expectedDays.all { it in weekdays } && days.filter { it.second }.map { it.first }.toSet() == expectedDays.toSet())
        if (label != target.label || time != target.time || description != target.time || !sameSchedule) failure("UNSUPPORTED", "Clock detail does not match selected alarm identity and recurrence")
    }
}

internal data class GoogleClockApplication(val window: Int, val pkg: String?, val active: Boolean, val focused: Boolean, val visible: Boolean = true)

internal object GoogleClockModalGraph {
    fun requireSingle(applications: List<GoogleClockApplication>, window: Int, activeCount: Int, focusedCount: Int, safeSystem: Boolean) {
        if (applications.size != 1 || applications.single() != GoogleClockApplication(window, GoogleClockProfile.PACKAGE, true, true) || activeCount != 1 || focusedCount != 1 || !safeSystem) failure("UNSUPPORTED", "Clock modal-safe window graph is not recognized")
    }
}

internal class GoogleClockAlarmDelete(val baseline: List<GoogleClockEntry>, val originalWindow: Int, val target: GoogleClockEntry, started: Long) {
    enum class Phase { BASELINE, SELECTED, DETAIL, DISPATCHED, RETURNED, RECREATED, COMPLETE, ABORTED }
    var phase = Phase.BASELINE
        private set
    var detailWindow: Int? = null
        private set
    private var effectDispatched = false
    private var deadline = started + GoogleClockReadback.TIMEOUT_MS
    private var lastTime = started
    private var selectedAt: Long? = null
    private var lastDetailAt: Long? = null
    private var since: Long? = null
    private var reboundWindow: Int? = null
    val expected = GoogleClockCollection.expected(baseline, target, null)
    init {
        GoogleClockCollection.requireComplete(baseline, baseline.size, baseline.indices.toList())
        if (baseline.singleOrNull { it.key == target.key } != target) failure("UNSUPPORTED", "Clock deletion target is not the unique baseline model")
    }
    private fun requireState(value: Boolean, message: String) {
        if (!value) {
            phase = Phase.ABORTED
            failure(if (effectDispatched) "AMBIGUOUS_STATE" else "UNSUPPORTED", message)
        }
    }
    fun check(now: Long) {
        requireState(phase !in setOf(Phase.ABORTED, Phase.COMPLETE) && now >= lastTime && now < deadline, "Clock deletion transaction expired or changed phase; no retry")
        lastTime = now
    }
    fun baseline(values: List<GoogleClockEntry>, window: Int, now: Long): Boolean {
        check(now)
        requireState(phase == Phase.BASELINE && window == originalWindow && GoogleClockCollection.same(values, baseline), "Clock complete baseline changed before selection")
        val first = since ?: now.also { since = it }
        return now - first >= 200
    }
    fun select(values: List<GoogleClockEntry>, window: Int, now: Long) {
        requireState(baseline(values, window, now), "Clock selection requires a stable fresh complete baseline")
        phase = Phase.SELECTED
        selectedAt = now
        since = null
    }
    fun detail(window: Int, label: String, time: String, description: String, days: List<Pair<String, Boolean>>, now: Long): Boolean {
        check(now)
        requireState(phase in setOf(Phase.SELECTED, Phase.DETAIL, Phase.DISPATCHED) && window != originalWindow && (detailWindow == null || window == detailWindow), "Clock selected modal window changed")
        if (phase != Phase.DISPATCHED) requireState(selectedAt?.let { now - it < 2000 } == true, "Clock selected modal proof arrived too late")
        try { GoogleClockAlarmDetail.requireIdentity(target, label, time, description, days) } catch (e: BridgeFailure) { requireState(false, e.message ?: "Clock detail identity changed") }
        detailWindow = window
        lastDetailAt = now
        if (phase == Phase.DISPATCHED) return false
        if (phase == Phase.SELECTED) phase = Phase.DETAIL
        val first = since ?: now.also { since = it }
        return now - first >= DETAIL_STABLE_MS
    }
    fun dispatch(window: Int, now: Long) {
        check(now)
        requireState(phase == Phase.DETAIL && window == detailWindow && since?.let { now - it >= DETAIL_STABLE_MS } == true && lastDetailAt?.let { now - it <= 100 } == true && selectedAt?.let { now - it < 2000 } == true, "Clock Delete requires a fresh stable selected modal")
        phase = Phase.DISPATCHED
        effectDispatched = true
        deadline = now + GoogleClockReadback.TIMEOUT_MS
        since = null
    }
    fun returned(values: List<GoogleClockEntry>, window: Int, detailPresent: Boolean, now: Long): Boolean {
        check(now)
        requireState(phase in setOf(Phase.DISPATCHED, Phase.RETURNED) && window == originalWindow && !detailPresent && GoogleClockCollection.same(values, expected), "Clock deletion returned an unexpected or incomplete collection")
        if (phase == Phase.DISPATCHED) since = null
        phase = Phase.RETURNED
        val first = since ?: now.also { since = it }
        return now - first >= 200
    }
    fun recreate(now: Long) {
        check(now)
        requireState(phase == Phase.RETURNED && since?.let { now - it >= 200 } == true, "Clock recreation requires stable complete post-delete proof")
        phase = Phase.RECREATED
        since = null
        deadline = now + GoogleClockReadback.TIMEOUT_MS
    }
    fun complete(values: List<GoogleClockEntry>, window: Int, originalPresent: Boolean, detailPresent: Boolean, now: Long): Boolean {
        check(now)
        requireState(phase == Phase.RECREATED && window != originalWindow && window != detailWindow && !originalPresent && !detailPresent && (reboundWindow == null || reboundWindow == window) && GoogleClockCollection.same(values, expected), "Clock recreated native models do not prove the selected deletion")
        reboundWindow = window
        val first = since ?: now.also { since = it }
        if (now - first < 200) return false
        phase = Phase.COMPLETE
        return true
    }
    companion object { const val DETAIL_STABLE_MS = 700L }
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
