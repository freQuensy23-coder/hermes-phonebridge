package org.nous.phonebridge

import android.content.Context
import android.view.accessibility.AccessibilityNodeInfo
import org.json.JSONArray
import org.json.JSONObject
import java.util.concurrent.TimeUnit
import java.util.concurrent.locks.ReentrantLock

interface ClockUiAdapter {
    val name: String
    fun supported(version: String, code: Long): Set<String>
    fun reason(version: String, code: Long): String
    fun execute(service: BridgeAccessibility, op: String, args: Arguments): JSONObject
}

class SamsungClockAdapter : ClockUiAdapter {
    override val name = "samsung_clock"
    override fun supported(version: String, code: Long) = emptySet<String>()
    override fun reason(version: String, code: Long) = "No independently verified Samsung package/version selector profile; native typed intents remain available"
    override fun execute(service: BridgeAccessibility, op: String, args: Arguments): JSONObject = failure("UNSUPPORTED", "Samsung Clock UI version has no verified selector profile")
}

class AospClockAdapter : ClockUiAdapter {
    override val name = "aosp_deskclock_source"
    private val pkg = "com.android.deskclock"
    private fun id(value: String) = "$pkg:id/$value"
    override fun supported(version: String, code: Long) = emptySet<String>()
    override fun reason(version: String, code: Long) = "No independently verified AOSP package/version selector profile; native typed intents remain available"
    data class AlarmRow(val label: String, val time: String, val enabled: Boolean, val row: AccessibilityNodeInfo)
    private fun alarms(service: BridgeAccessibility): List<AlarmRow> {
        val root = service.safeRoot(pkg)
        val all = service.nodes(root)
        val recycler = all.singleOrNull { it.viewIdResourceName == id("alarms_recycler_view") && it.isVisibleToUser } ?: failure("UNSUPPORTED", "Open the supported AOSP alarms screen")
        val toggles = service.nodes(recycler).filter { it.viewIdResourceName == id("onoff") && it.isVisibleToUser }
        if (toggles.isEmpty() && all.none { it.viewIdResourceName == id("alarms_empty_view") && it.isVisibleToUser }) failure("UNSUPPORTED", "Unknown alarms screen schema")
        val rows = toggles.map { toggle ->
            requireBridge(toggle.isCheckable && toggle.isEnabled, "Unknown alarm switch schema")
            var parent = toggle.parent ?: failure("UNSUPPORTED", "Alarm row unavailable")
            repeat(4) {
                val children = service.nodes(parent)
                val time = children.singleOrNull { it.viewIdResourceName == id("digital_clock") && it.isVisibleToUser }
                val labels = children.filter { it.viewIdResourceName in setOf(id("label"), id("edit_label")) && it.isVisibleToUser }
                if (time != null && children.count { it.viewIdResourceName == id("onoff") && it.isVisibleToUser } == 1) {
                    return@map AlarmRow(labels.singleOrNull()?.text?.toString() ?: "", time.text?.toString() ?: failure("UNSUPPORTED", "Unreadable alarm time"), toggle.isChecked, parent)
                }
                parent = parent.parent ?: failure("UNSUPPORTED", "Unknown alarm row layout")
            }
            failure("UNSUPPORTED", "Alarm row nesting exceeds profile")
        }
        if (rows.groupBy { it.label to it.time }.values.any { it.size != 1 }) failure("UNSUPPORTED", "Duplicate alarm labels/times cannot be safely selected")
        return rows
    }
    private fun identity(label: String, time: String): String = java.security.MessageDigest.getInstance("SHA-256").digest(JSONObject().put("package", pkg).put("label", label).put("time", time).toString().toByteArray()).joinToString("") { "%02x".format(it) }
    private fun alarmData(rows: List<AlarmRow>) = JSONArray(rows.map { JSONObject().put("id", identity(it.label, it.time)).put("label", it.label).put("time", it.time).put("enabled", it.enabled) })
    private fun selected(service: BridgeAccessibility, label: String, time: String): AlarmRow = alarms(service).singleOrNull { it.label == label && it.time == time } ?: failure("NOT_FOUND", "Exact visible alarm label/time not found")
    private fun timer(service: BridgeAccessibility): JSONObject {
        val root = service.safeRoot(pkg)
        val nodes = service.nodes(root)
        if (nodes.none { it.viewIdResourceName == id("timer_view") && it.isVisibleToUser }) failure("UNSUPPORTED", "Open the supported AOSP timer screen")
        val label = service.exact(root, id("timer_label")).text?.toString() ?: ""
        val time = service.exact(root, id("timer_time_text")).text?.toString() ?: failure("UNSUPPORTED", "Unreadable timer")
        val fab = service.exact(root, id("fab"))
        val state = when (fab.contentDescription?.toString()) {
            "Stop" -> "running"
            "Start" -> {
                val add = service.exact(root, id("reset_add"))
                if (add.text?.toString() != "Reset") failure("UNSUPPORTED", "Unknown paused timer schema")
                "paused_or_reset"
            }
            else -> failure("UNSUPPORTED", "Timer state or locale unsupported")
        }
        return JSONObject().put("id", identity(label, time)).put("label", label).put("time", time).put("state", state).put("identity_scope", "fresh_visible_snapshot")
    }
    override fun execute(service: BridgeAccessibility, op: String, args: Arguments): JSONObject {
        if (op !in supported("", 0)) failure("UNSUPPORTED", reason("", 0))
        args.only("package", "id", "label", "time")
        if (op == "clock.alarms.list") return UiThread.call { JSONObject().put("alarms", alarmData(alarms(service))).put("scope", "visible_rows").put("complete", false) }
        if (op == "clock.timers.list") return UiThread.call { JSONObject().put("timers", JSONArray().put(timer(service))).put("scope", "visible_timer").put("complete", false) }
        val targetId = args.string("id", 128, false)
        val target = if (targetId != null) UiThread.call {
            if (op.startsWith("clock.alarms.")) {
                val row = alarms(service).singleOrNull { identity(it.label, it.time) == targetId } ?: failure("NOT_FOUND", "Fresh visible alarm ID not found")
                row.label to row.time
            } else {
                val value = timer(service)
                if (value.getString("id") != targetId) failure("NOT_FOUND", "Timer snapshot changed; request a fresh list")
                value.getString("label") to value.getString("time")
            }
        } else args.string("label", 256)!! to args.string("time", 128)!!
        val label = target.first
        val time = target.second
        args.string("label", 256, false)?.let { requireBridge(it == label, "Selected label does not match ID") }
        args.string("time", 128, false)?.let { requireBridge(it == time, "Selected time does not match ID") }
        if (op.startsWith("clock.alarms.")) {
            val desired = when (op) {
                "clock.alarms.enable" -> true
                "clock.alarms.disable" -> false
                "clock.alarms.delete" -> null
                else -> failure("UNSUPPORTED", "Unknown alarm UI operation")
            }
            val before = UiThread.call { selected(service, label, time).enabled }
            if (desired != null && before == desired) return JSONObject().put("verified", true).put("outcome", "unchanged").put("enabled", desired)
            UiThread.call {
                val row = selected(service, label, time)
                if (desired != null) {
                    val toggle = service.exact(row.row, id("onoff"))
                    if (toggle.isChecked != desired) service.click(toggle)
                } else {
                    val deletion = service.nodes(row.row).singleOrNull { it.viewIdResourceName == id("delete") && it.isVisibleToUser }
                    if (deletion == null) service.click(service.exact(row.row, id("arrow")))
                }
            }
            if (desired == null) {
                var clicked = false
                repeat(8) {
                    if (!clicked) {
                        clicked = UiThread.call {
                            val row = selected(service, label, time)
                            val button = service.nodes(row.row).singleOrNull { it.viewIdResourceName == id("delete") && it.isVisibleToUser }
                            if (button != null) { service.click(button); true } else false
                        }
                        if (!clicked) Thread.sleep(100)
                    }
                }
                if (!clicked) failure("AMBIGUOUS_STATE", "Alarm expanded but delete selector unavailable; no retry")
            }
            repeat(12) {
                val verified = UiThread.call {
                    val rows = alarms(service)
                    if (desired == null) rows.none { it.label == label && it.time == time } else rows.singleOrNull { it.label == label && it.time == time }?.enabled == desired
                }
                if (verified) return JSONObject().put("verified", true).put("outcome", if (desired == null) "deleted" else "changed").put("scope", "visible_row_readback").put("label", label).put("time", time)
                Thread.sleep(100)
            }
            failure("AMBIGUOUS_STATE", "Alarm action dispatched but read-back did not confirm; no retry")
        }
        val before = UiThread.call { timer(service) }
        requireBridge(before.getString("label") == label && before.getString("time") == time, "Timer identity changed")
        val targetState = when (op) {
            "clock.timers.pause" -> { if (before.getString("state") != "running") failure("UNSUPPORTED", "Timer is not known running"); "paused_or_reset" }
            "clock.timers.resume" -> { if (before.getString("state") != "paused_or_reset") failure("UNSUPPORTED", "Timer is not known paused/reset"); "running" }
            "clock.timers.cancel" -> "absent"
            else -> failure("UNSUPPORTED", "Unknown timer UI operation")
        }
        UiThread.call {
            val fresh = timer(service)
            requireBridge(fresh.getString("label") == label && fresh.getString("state") == before.getString("state"), "Timer state changed")
            val root = service.safeRoot(pkg)
            if (targetState == "absent") {
                val button = service.exact(root, id("left_button"))
                if (button.contentDescription?.toString() != "Delete") failure("UNSUPPORTED", "Unknown timer deletion control")
                service.click(button)
            } else service.click(service.exact(root, id("fab")))
        }
        repeat(12) {
            val verified = UiThread.call {
                if (targetState == "absent") {
                    val root = service.safeRoot(pkg)
                    val nodes = service.nodes(root)
                    val setup = nodes.any { it.viewIdResourceName == id("timer_setup") && it.isVisibleToUser }
                    val oldLabelGone = nodes.none { it.viewIdResourceName == id("timer_label") && it.isVisibleToUser && it.text?.toString() == label }
                    setup && oldLabelGone
                } else {
                    val fresh = timer(service)
                    fresh.getString("label") == label && fresh.getString("state") == targetState
                }
            }
            if (verified) return JSONObject().put("verified", true).put("outcome", "changed").put("state", targetState).put("scope", "visible_timer_readback")
            Thread.sleep(100)
        }
        failure("AMBIGUOUS_STATE", "Timer action dispatched but read-back did not confirm; no retry")
    }
    companion object {
        val operations = setOf("clock.alarms.list", "clock.alarms.enable", "clock.alarms.disable", "clock.alarms.delete", "clock.timers.list", "clock.timers.cancel", "clock.timers.pause", "clock.timers.resume")
    }
}

class ClockAdapters(private val context: Context) {
    fun forPackage(pkg: String): ClockUiAdapter = when (pkg) {
        "com.google.android.deskclock" -> GoogleClockAdapter(context)
        "com.sec.android.app.clockpackage" -> SamsungClockAdapter()
        "com.android.deskclock" -> AospClockAdapter()
        else -> failure("UNSUPPORTED", "Clock package is not supported")
    }
    fun capabilities(): Set<String> = NativeClocks(context).installed().flatMap { pkg ->
        val info = context.packageManager.getPackageInfo(pkg, 0)
        forPackage(pkg).supported(info.versionName ?: "", info.longVersionCode)
    }.toSet()
    fun execute(op: String, args: Arguments): JSONObject {
        Access.unlocked(context)
        val service = BridgeAccessibility.current ?: failure("PERMISSION_REQUIRED", "Clock UI adapters require Accessibility")
        val pkg = args.string("package", 256)!!
        val adapter = forPackage(pkg)
        val info = try { context.packageManager.getPackageInfo(pkg, 0) } catch (_: android.content.pm.PackageManager.NameNotFoundException) { failure("NOT_FOUND", "Clock package not installed") }
        if (op !in adapter.supported(info.versionName ?: "", info.longVersionCode)) failure("UNSUPPORTED", adapter.reason(info.versionName ?: "", info.longVersionCode))
        if (!serial.tryLock(1, TimeUnit.SECONDS)) failure("QUEUE_FULL", "Clock UI adapter is busy")
        try {
            return adapter.execute(service, op, args).put("backend", "ui").put("screen_required", true).put("package", pkg).put("adapter", adapter.name)
        } finally { serial.unlock() }
    }
    companion object { private val serial = ReentrantLock() }
}
