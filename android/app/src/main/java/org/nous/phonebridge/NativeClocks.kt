package org.nous.phonebridge

import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.provider.AlarmClock
import org.json.JSONArray
import org.json.JSONObject

class NativeClocks(private val context: Context) {
    private val pm = context.packageManager
    private fun resolves(pkg: String, action: String): Boolean = pm.queryIntentActivities(Intent(action).setPackage(pkg), PackageManager.MATCH_DEFAULT_ONLY).any { it.activityInfo.exported && it.activityInfo.enabled }
    fun installed(): List<String> = packages.filter { try { pm.getPackageInfo(it, 0); true } catch (_: PackageManager.NameNotFoundException) { false } }
    fun nativeCapabilities(): Set<String> = actions.filter { (_, action) -> installed().any { resolves(it, action) } }.keys
    fun providers(): JSONObject = JSONObject().put("providers", JSONArray(installed().map { pkg ->
        val info = pm.getPackageInfo(pkg, 0)
        val adapter = ClockAdapters(context).forPackage(pkg)
        JSONObject().put("package", pkg).put("name", pm.getApplicationLabel(pm.getApplicationInfo(pkg, 0)).toString()).put("version", info.versionName ?: "").put("version_code", info.longVersionCode).put("adapter", adapter.name).put("native_actions", JSONArray(actions.filter { resolves(pkg, it.value) }.keys.sorted())).put("ui_actions", JSONArray(adapter.supported(info.versionName ?: "", info.longVersionCode).sorted())).put("screen_required", true).put("ui_support", adapter.reason(info.versionName ?: "", info.longVersionCode))
    }))
    fun intent(op: String, args: Arguments): Intent {
        val action = actions[op] ?: failure("UNSUPPORTED", "Operation requires a clock UI adapter")
        val requested = args.string("package", 256, false)
        val candidates = installed().filter { resolves(it, action) }
        val pkg = if (requested != null) {
            requireBridge(requested in packages, "Only known clock packages are allowed")
            if (requested !in candidates) failure("UNSUPPORTED", "Clock does not resolve this typed action")
            requested
        } else {
            candidates.singleOrNull() ?: failure("VALIDATION_ERROR", "Select an explicit package when zero or multiple clock providers resolve the action")
        }
        val intent = Intent(action).setPackage(pkg)
        when (op) {
            "clock.alarms.create" -> {
                args.only("package", "hour", "minute", "label", "days")
                intent.putExtra(AlarmClock.EXTRA_HOUR, args.int("hour", 0, 23)).putExtra(AlarmClock.EXTRA_MINUTES, args.int("minute", 0, 59)).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                args.string("label", 256, false)?.let { intent.putExtra(AlarmClock.EXTRA_MESSAGE, it) }
                if (args.json.has("days")) {
                    val days = args.json.optJSONArray("days") ?: failure("VALIDATION_ERROR", "days must be an array")
                    intent.putIntegerArrayListExtra(AlarmClock.EXTRA_DAYS, ArrayList(calendarDays(days).toList()))
                }
            }
            "clock.alarms.dismiss" -> {
                args.only("package", "label", "hour", "minute")
                val label = args.string("label", 256, false)
                requireBridge(label == null || (!args.json.has("hour") && !args.json.has("minute")), "Select alarm by label or time, not both")
                if (label != null) {
                    intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_LABEL).putExtra(AlarmClock.EXTRA_MESSAGE, label)
                } else if (args.json.has("hour") || args.json.has("minute")) {
                    intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_TIME).putExtra(AlarmClock.EXTRA_HOUR, args.int("hour", 0, 23)).putExtra(AlarmClock.EXTRA_MINUTES, args.int("minute", 0, 59))
                } else intent.putExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE, AlarmClock.ALARM_SEARCH_MODE_ALL)
            }
            "clock.alarms.snooze" -> {
                args.only("package", "minutes")
                if (args.json.has("minutes")) intent.putExtra(AlarmClock.EXTRA_ALARM_SNOOZE_DURATION, args.int("minutes", 1, 120))
            }
            "clock.timers.create" -> {
                args.only("package", "seconds", "label")
                intent.putExtra(AlarmClock.EXTRA_LENGTH, args.int("seconds", 1, 86400)).putExtra(AlarmClock.EXTRA_SKIP_UI, true)
                args.string("label", 256, false)?.let { intent.putExtra(AlarmClock.EXTRA_MESSAGE, it) }
            }
            "clock.timers.dismiss" -> {
                args.only("package", "uri")
                args.string("uri", 2048, false)?.let {
                    val uri = Uri.parse(it)
                    requireBridge(uri.scheme == "content" && uri.authority in setOf(pkg, "com.android.deskclock") && uri.fragment == null && uri.query == null && uri.path?.matches(Regex("/timers?/[0-9]+")) == true, "Timer URI must be a known clock content timer URI")
                    intent.data = uri
                }
            }
            else -> args.only("package")
        }
        return intent
    }
    fun execute(op: String, args: Arguments): JSONObject {
        if (op == "clock.providers") { args.only(); return providers() }
        if (op !in actions) return ClockAdapters(context).execute(op, args)
        val intent = intent(op, args)
        Access.foreground(context)
        UiThread.call { (BridgeAccessibility.current ?: context).startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        return JSONObject().put("outcome", "dispatched").put("verified", false).put("package", intent.`package`).put("backend", "native_intent").apply {
            if (op == "clock.alarms.dismiss") put("repeating_alarm", "next_occurrence_skipped")
            if (op == "clock.timers.dismiss" && intent.data == null) put("scope", "expired_timers_only")
        }
    }
    companion object {
        val packages = listOf("com.google.android.deskclock", "com.sec.android.app.clockpackage", "com.android.deskclock")
        val actions = linkedMapOf("clock.alarms.create" to AlarmClock.ACTION_SET_ALARM, "clock.alarms.dismiss" to AlarmClock.ACTION_DISMISS_ALARM, "clock.alarms.snooze" to AlarmClock.ACTION_SNOOZE_ALARM, "clock.alarms.show" to AlarmClock.ACTION_SHOW_ALARMS, "clock.timers.create" to AlarmClock.ACTION_SET_TIMER, "clock.timers.dismiss" to AlarmClock.ACTION_DISMISS_TIMER, "clock.timers.show" to AlarmClock.ACTION_SHOW_TIMERS)
    }
}
