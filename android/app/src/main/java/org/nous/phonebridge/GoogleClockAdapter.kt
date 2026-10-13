package org.nous.phonebridge

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.graphics.Rect
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject

class GoogleClockAdapter(private val context: Context? = null) : ClockUiAdapter {
    override val name = "google_clock_8_2_exact"
    override fun supported(version: String, code: Long): Set<String> =
        if (version == GoogleClockProfile.VERSION && code == GoogleClockProfile.CODE && context != null && GoogleClockProfile.installed(context)) GoogleClockProfile.operations else emptySet()
    override fun reason(version: String, code: Long) = "Source-derived exact signed 8.2 alarm cards and classic/Gastown timer profiles; English 24-hour UI, API31/35, complete non-scrolling collections only; current explicit display selection, no vendor IDs; CI and physical runtime unverified"

    private data class Row(val value: GoogleClockEntry, val node: AccessibilityNodeInfo, val control: AccessibilityNodeInfo, val deletion: AccessibilityNodeInfo?)
    private data class Collection(val rows: List<Row>, val window: Int) {
        val values get() = rows.map { it.value }
    }
    private data class Snapshot(val values: List<GoogleClockEntry>, val window: Int)
    private fun id(value: String) = "${GoogleClockProfile.PACKAGE}:id/$value"
    private fun requireSchema(value: Boolean, message: String) {
        if (!value) failure("UNSUPPORTED", message)
    }
    private fun gate() = GoogleClockProfile.requireInstalled(context ?: failure("UNSUPPORTED", "Clock profile context is required"))

    private fun completeNodes(node: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = ArrayList<AccessibilityNodeInfo>()
        fun visit(current: AccessibilityNodeInfo, depth: Int) {
            requireSchema(depth <= 30 && result.size < 400 && current.childCount <= 100, "Clock tree exceeds complete traversal bound")
            requireSchema(current.refresh(), "Clock node cannot be freshly reacquired")
            requireSchema(current.childCount <= 100, "Clock refreshed child bound exceeded")
            requireSchema(!current.isPassword && current.packageName?.toString() == GoogleClockProfile.PACKAGE, "Clock tree contains excluded or foreign nodes")
            result.add(current)
            for (index in 0 until current.childCount) visit(current.getChild(index) ?: failure("UNSUPPORTED", "Clock tree child unavailable"), depth + 1)
        }
        visit(node, 0)
        return result
    }

    private fun activeRoot(service: BridgeAccessibility, detail: Boolean = false): AccessibilityNodeInfo {
        val root = service.safeRoot(GoogleClockProfile.PACKAGE)
        val windows = service.windows
        val active = windows.singleOrNull { it.id == root.windowId && it.isActive && it.isFocused && it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        requireSchema(active != null, "Clock requires one focused application window")
        requireSchema(windows.count { it.isActive } == 1 && windows.count { it.isFocused } == 1, "Clock window focus graph is ambiguous")
        requireSchema(windows.filter { it.type != AccessibilityWindowInfo.TYPE_APPLICATION }.all { it.type == AccessibilityWindowInfo.TYPE_SYSTEM && !it.isActive && !it.isFocused && it.root?.packageName?.toString() == "com.android.systemui" }, "Clock is covered by an unknown overlay or input window")
        val application = windows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        val expectedApplications = if (detail) 2 else 1
        requireSchema(application.size == expectedApplications, "Clock window graph is not recognized: expected=$expectedApplications, actual=${application.size}, active=${root.windowId}, windows=${application.take(8).joinToString { "${it.id}:${it.root?.packageName}:${it.isActive}:${it.isFocused}" }}")
        application.forEach { window ->
            val windowRoot = window.root ?: failure("UNSUPPORTED", "Clock window root unavailable")
            val nodes = completeNodes(windowRoot)
            requireSchema(visible(nodes, "body").isEmpty() && visible(nodes, "dismiss_button").isEmpty(), "Clock promotion popup is not a collection host")
            if (window.id != root.windowId) requireSchema(!window.isActive && !window.isFocused && window.layer < active!!.layer && nodes.count { it.viewIdResourceName == id("alarm_recycler_view") } == 1 && visible(nodes, "clock").isEmpty() && visible(nodes, "save_button").isEmpty(), "Clock inactive detail host is not recognized")
        }
        completeNodes(root)
        return root
    }

    private fun visible(nodes: List<AccessibilityNodeInfo>, resource: String) = nodes.filter { it.viewIdResourceName == id(resource) && it.isVisibleToUser }
    private fun one(nodes: List<AccessibilityNodeInfo>, resource: String): AccessibilityNodeInfo = visible(nodes, resource).singleOrNull() ?: failure("UNSUPPORTED", "Missing or duplicate Clock $resource")
    private fun boundedText(node: AccessibilityNodeInfo, description: Boolean = false, empty: Boolean = false): String {
        val value = (if (description) node.contentDescription else node.text)?.toString() ?: if (empty) "" else failure("UNSUPPORTED", "Clock identity is unreadable")
        requireSchema(value.length <= 256 && (empty || value.isNotEmpty()) && '\u0000' !in value && '\u2026' !in value && value.none { it.isSurrogate() }, "Clock identity is truncated or exceeds profile")
        return value
    }
    private fun clickable(node: AccessibilityNodeInfo) {
        requireSchema(node.isVisibleToUser && node.isEnabled && node.isClickable && !node.isPassword, "Clock control is not safely clickable")
    }
    private fun contained(node: AccessibilityNodeInfo, parent: AccessibilityNodeInfo) {
        val bounds = Rect().also { node.getBoundsInScreen(it) }
        val container = Rect().also { parent.getBoundsInScreen(it) }
        requireSchema(!bounds.isEmpty && container.contains(bounds), "Clock row is clipped")
    }
    private fun rowFor(node: AccessibilityNodeInfo, recycler: AccessibilityNodeInfo, alarm: Boolean): AccessibilityNodeInfo {
        var current = node
        repeat(30) {
            requireSchema(current != recycler, "Clock row ancestry is unknown")
            if (alarm && current.viewIdResourceName == id("alarm_card")) return current
            if (!alarm && current.collectionItemInfo != null) return current
            current = current.parent ?: failure("UNSUPPORTED", "Clock row parent is unavailable")
        }
        failure("UNSUPPORTED", "Clock row ancestry exceeds bound")
    }
    private fun index(row: AccessibilityNodeInfo, recycler: AccessibilityNodeInfo, alarm: Boolean, columns: Int, count: Int): Int {
        var current = row
        repeat(30) {
            requireSchema(current != recycler, "Clock collection item metadata unavailable")
            current.collectionItemInfo?.let {
                val ordinary = it.rowSpan == 1 && it.columnSpan == 1
                val singleTimer = !alarm && count == 1 && it.rowIndex == 0 && it.columnIndex == 0 && ((it.rowSpan == 1 && it.columnSpan == 2) || (it.rowSpan == 2 && it.columnSpan == 1))
                requireSchema(!it.isHeading && (ordinary || singleTimer) && it.columnIndex in 0 until columns && !it.isSelected, "Unsupported Clock collection item")
                return it.rowIndex * columns + it.columnIndex
            }
            current = current.parent ?: failure("UNSUPPORTED", "Clock item ancestry unavailable")
        }
        failure("UNSUPPORTED", "Clock item ancestry exceeds bound")
    }

    private fun collection(service: BridgeAccessibility, alarm: Boolean, root: AccessibilityNodeInfo = activeRoot(service)): Collection {
        val all = completeNodes(root)
        requireSchema(visible(all, "clock").isEmpty() && visible(all, "save_button").isEmpty() && visible(all, "loading_indicator").isEmpty(), "Clock detail or loading surface is active")
        val resource = id(if (alarm) "alarm_recycler_view" else "timer_view")
        val recycler = all.filter { it.viewIdResourceName == resource }.singleOrNull() ?: failure("UNSUPPORTED", "Clock complete collection anchor is unavailable")
        val timerSurface = if (alarm) null else GoogleClockTimerSurface.fromClass(recycler.className?.toString() ?: "")
        if (alarm) requireSchema(recycler.className?.toString() == "android.widget.GridView", "Clock alarm collection is not the source-bound GridLayout accessibility schema")
        val info = recycler.collectionInfo ?: failure("UNSUPPORTED", "Clock complete collection count is unavailable")
        val count = GoogleClockCollection.count(info.rowCount, info.columnCount, alarm)
        if (timerSurface == GoogleClockTimerSurface.CLASSIC) requireSchema(count == 0 || info.rowCount == 1 || info.columnCount == 1, "Classic Clock timer collection must be linear")
        requireSchema(info.selectionMode == AccessibilityNodeInfo.CollectionInfo.SELECTION_MODE_NONE && !info.isHierarchical, "Unsupported Clock collection metadata")
        requireSchema(count == 0 || recycler.isVisibleToUser, "Nonempty Clock collection is hidden")
        requireSchema(recycler.actionList.none { it.id in setOf(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, android.R.id.accessibilityActionScrollUp, android.R.id.accessibilityActionScrollDown, android.R.id.accessibilityActionScrollLeft, android.R.id.accessibilityActionScrollRight) }, "Scrollable Clock collections are excluded")
        val children = completeNodes(recycler)
        val anchors = visible(children, if (alarm) "onoff" else "timer_label")
        val rows = anchors.map { anchor ->
            val row = rowFor(anchor, recycler, alarm)
            contained(row, recycler)
            val nodes = completeNodes(row)
            if (alarm) {
                requireSchema(one(nodes, "onoff") == anchor && anchor.isCheckable && anchor.className?.toString() == "android.widget.Switch" && row.className?.toString() == "androidx.cardview.widget.CardView", "Unknown Clock alarm card/switch schema")
                clickable(anchor)
                val clock = one(nodes, "digital_clock")
                val time = boundedText(clock)
                val labelNode = visible(nodes, "label").singleOrNull()
                requireSchema(visible(nodes, "label").size <= 1, "Unknown Clock alarm label")
                val label = labelNode?.let { boundedText(it) } ?: ""
                if (labelNode != null) requireSchema(boundedText(labelNode, true) == "Label $label", "Clock alarm full label is not corroborated")
                GoogleClockIdentity.alarm(time, boundedText(clock, true), label, boundedText(row, true), boundedText(anchor, true))
                requireSchema(listOf("pause_resume_text", "watch_alarm_icon", "preemptive_dismiss", "workflow").all { visible(nodes, it).isEmpty() }, "Paused-date, watch, firing or routine alarms are excluded")
                val days = visible(nodes, "days_of_week").singleOrNull()
                val upcoming = visible(nodes, "upcoming_instance_label").singleOrNull()
                requireSchema(visible(nodes, "days_of_week").size <= 1 && visible(nodes, "upcoming_instance_label").size <= 1, "Unknown Clock alarm scheduling graph")
                requireSchema((days != null && upcoming == null) || (days == null && upcoming != null && boundedText(upcoming) in setOf("Today", "Tomorrow", "Not scheduled")), "Unknown or date-scheduled Clock alarm")
                Row(GoogleClockEntry(label, time, if (anchor.isChecked) "enabled" else "disabled", days?.let { boundedText(it, true) } ?: ""), row, anchor, null)
            } else {
                requireSchema(one(nodes, "timer_label") == anchor, "Unknown Clock timer row")
                val label = boundedText(anchor)
                requireSchema(boundedText(anchor, true) == label, "Only full named Clock timer identities are supported")
                val surface = timerSurface ?: failure("UNSUPPORTED", "Clock timer surface is missing")
                val other = GoogleClockTimerSurface.values().single { it != surface }
                requireSchema(listOf(other.action, other.reset, other.deletion, other.addition).all { visible(nodes, it).isEmpty() }, "Clock timer row mixes incompatible surfaces")
                val action = one(nodes, surface.action)
                val reset = one(nodes, surface.reset)
                val deletion = one(nodes, surface.deletion)
                one(nodes, "timer_text")
                val addition = visible(nodes, surface.addition)
                requireSchema(addition.size <= 1, "Clock timer addition control is ambiguous")
                if (surface == GoogleClockTimerSurface.GASTOWN) one(nodes, surface.addition)
                else if (addition.isNotEmpty()) requireSchema(boundedText(addition.single(), true) == "Add 1 minute", "Classic Clock timer primary control is not addition")
                clickable(action)
                clickable(reset)
                clickable(deletion)
                contained(action, row)
                contained(reset, row)
                contained(deletion, row)
                val state = surface.state(label, boundedText(action, true), boundedText(reset, true), boundedText(deletion, true))
                Row(GoogleClockEntry(label, "", state, ""), row, action, deletion)
            }
        }
        requireSchema(rows.map { it.node }.distinct().size == rows.size, "Clock rows overlap")
        requireSchema(visible(children, if (alarm) "alarm_card" else timerSurface!!.action).size == rows.size, "Clock collection contains unknown rows")
        if (rows.isEmpty()) {
            if (alarm) requireSchema(boundedText(one(all, "alarm_empty_view")) == "No Alarms", "Clock empty alarm state unavailable")
            else one(all, "timer_setup")
        } else requireSchema(visible(all, if (alarm) "alarm_empty_view" else "timer_setup").isEmpty(), "Clock list and setup/empty states overlap")
        GoogleClockCollection.requireComplete(rows.map { it.value }, count, rows.map { index(it.node, recycler, alarm, info.columnCount, count) })
        return Collection(rows, root.windowId)
    }

    private fun snapshot(service: BridgeAccessibility, alarm: Boolean): Snapshot {
        gate()
        return UiThread.call { collection(service, alarm).let { Snapshot(it.values, it.window) } }
    }
    private fun awaitSnapshot(service: BridgeAccessibility, alarm: Boolean, expected: List<GoogleClockEntry>, deadline: Long, expectedWindow: Int): Snapshot {
        var first: Long? = null
        var last: Snapshot? = null
        repeat(20) {
            if (SystemClock.elapsedRealtime() >= deadline) failure("AMBIGUOUS_STATE", "Clock readback deadline exceeded; no effect retry")
            val current = snapshot(service, alarm)
            requireSchema(current.window == expectedWindow, "Clock window changed during readback")
            if (GoogleClockCollection.same(current.values, expected)) {
                val now = SystemClock.elapsedRealtime()
                if (last?.window != current.window) first = now
                if (first == null) first = now
                if (now - first!! >= 200) return current
                last = current
            } else {
                first = null
                last = null
            }
            Thread.sleep(100)
        }
        failure("AMBIGUOUS_STATE", "Clock complete readback could not prove the effect; no retry")
    }
    private fun rebind(service: BridgeAccessibility, alarm: Boolean, expected: List<GoogleClockEntry>, window: Int, deadline: Long) {
        gate()
        UiThread.call {
            val fresh = collection(service, alarm)
            requireSchema(fresh.window == window && GoogleClockCollection.same(fresh.values, expected), "Clock changed before model rebind")
            requireSchema(service.performGlobalAction(AccessibilityService.GLOBAL_ACTION_BACK), "Clock activity close refused")
        }
        var left = false
        repeat(10) {
            if (!left && SystemClock.elapsedRealtime() < deadline) {
                left = UiThread.call {
                    Access.unlocked(service)
                    service.rootInActiveWindow?.packageName?.toString()?.let { it != GoogleClockProfile.PACKAGE } == true
                }
                if (!left) Thread.sleep(100)
            }
        }
        requireSchema(left, "Clock activity did not close for model rebind")
        gate()
        val intent = NativeClocks(context!!).intent(if (alarm) "clock.alarms.show" else "clock.timers.show", Arguments(JSONObject().put("package", GoogleClockProfile.PACKAGE)))
        UiThread.call { Access.unlocked(service); service.startActivity(intent.addFlags(android.content.Intent.FLAG_ACTIVITY_NEW_TASK)) }
        var returnedWindow: Int? = null
        repeat(10) {
            if (returnedWindow == null && SystemClock.elapsedRealtime() < deadline) {
                returnedWindow = UiThread.call {
                    Access.unlocked(service)
                    service.rootInActiveWindow?.takeIf { it.packageName?.toString() == GoogleClockProfile.PACKAGE && it.windowId != window }?.windowId
                }
                if (returnedWindow == null) Thread.sleep(100)
            }
        }
        awaitSnapshot(service, alarm, expected, deadline, returnedWindow ?: failure("UNSUPPORTED", "Clock did not return in a new model-backed window"))
    }
    private fun detail(service: BridgeAccessibility, before: Snapshot, target: GoogleClockEntry): AccessibilityNodeInfo {
        val root = activeRoot(service, true)
        val nodes = completeNodes(root)
        requireSchema(boundedText(one(nodes, "clock")) == target.time && boundedText(one(nodes, "alarm_label"), empty = true) == target.label, "Clock detail does not match selected alarm")
        requireSchema(visible(nodes, "alarm_label_edit").isEmpty() && visible(nodes, "loading_indicator").isEmpty() && visible(nodes, "pause_alarm_cancel_button").isEmpty(), "Clock detail is loading, editing or date-paused")
        one(nodes, "repeat_days")
        val save = one(nodes, "save_button")
        requireSchema(boundedText(save) == "Save", "Unknown Clock detail save schema")
        val underlying = service.windows.singleOrNull { it.id == before.window && it.id != root.windowId }?.root ?: failure("UNSUPPORTED", "Clock original collection is not exposed under detail")
        requireSchema(GoogleClockCollection.same(collection(service, true, underlying).values, before.values), "Clock collection changed while opening detail")
        val deletion = one(nodes, "delete_button")
        requireSchema(boundedText(deletion) == "Delete", "Unknown Clock detail delete control")
        clickable(deletion)
        return deletion
    }
    private fun openDetail(service: BridgeAccessibility, before: Snapshot, target: GoogleClockEntry) {
        gate()
        UiThread.call {
            val fresh = collection(service, true)
            requireSchema(fresh.window == before.window && GoogleClockCollection.same(fresh.values, before.values), "Clock selection changed before detail")
            val row = fresh.rows.single { it.value.key == target.key }
            clickable(row.node)
            service.click(row.node)
        }
        val deadline = SystemClock.elapsedRealtime() + 2000
        repeat(20) {
            if (SystemClock.elapsedRealtime() < deadline) {
                val loaded = UiThread.call {
                    val root = service.safeRoot(GoogleClockProfile.PACKAGE)
                    val nodes = completeNodes(root)
                    visible(nodes, "clock").size == 1 && visible(nodes, "alarm_label").size == 1 && visible(nodes, "loading_indicator").isEmpty()
                }
                if (loaded) { UiThread.call { detail(service, before, target) }; return }
                Thread.sleep(100)
            }
        }
        failure("UNSUPPORTED", "Clock detail identity was not exposed; no alarm mutation dispatched")
    }

    private fun awaitDetailClose(service: BridgeAccessibility, target: GoogleClockEntry, originalWindow: Int, detailWindow: Int, deadline: Long) {
        repeat(20) {
            if (SystemClock.elapsedRealtime() >= deadline) failure("AMBIGUOUS_STATE", "Clock detail did not close after deletion")
            gate()
            val closed = UiThread.call {
                val root = service.safeRoot(GoogleClockProfile.PACKAGE)
                requireSchema(root.windowId in setOf(originalWindow, detailWindow), "Clock window changed after deletion")
                val nodes = completeNodes(root)
                if (root.windowId == originalWindow && visible(nodes, "clock").isEmpty() && visible(nodes, "save_button").isEmpty()) {
                    activeRoot(service)
                    true
                } else {
                    activeRoot(service, true)
                    requireSchema(root.windowId == detailWindow && boundedText(one(nodes, "clock")) == target.time && boundedText(one(nodes, "alarm_label"), empty = true) == target.label, "Unknown Clock deletion transition")
                    false
                }
            }
            if (closed) return
            Thread.sleep(100)
        }
        failure("AMBIGUOUS_STATE", "Clock deletion sheet remained open; no retry")
    }

    override fun execute(service: BridgeAccessibility, op: String, args: Arguments): JSONObject {
        gate()
        if (op !in GoogleClockProfile.operations) failure("UNSUPPORTED", "Unknown Google Clock UI operation")
        args.only("package", "id", "label", "time")
        requireBridge(args.string("package", 256) == GoogleClockProfile.PACKAGE, "Google Clock package is required")
        if (args.json.has("id")) failure("UNSUPPORTED", "Google Clock exposes no durable vendor ID; use explicit current label/time selection")
        val alarm = op.startsWith("clock.alarms.")
        val before = snapshot(service, alarm)
        if (op.endsWith(".list")) {
            requireBridge(!args.json.has("label") && !args.json.has("time"), "List does not accept selection fields")
            return JSONObject().put(if (alarm) "alarms" else "timers", JSONArray(before.values.map {
                JSONObject().put("id", JSONObject.NULL).put("label", it.label).put("time", if (alarm) it.time else JSONObject.NULL).put("state", it.state).put("schedule", it.schedule).put("identity_scope", if (alarm) "current_unique_label_time" else "current_unique_named_label").apply { if (alarm) put("enabled", it.state == "enabled") }
            })).put("complete", true).put("scope", "complete_current_collection").put("vendor_ids_available", false)
        }
        val label = args.string("label", 256)!!
        val time = if (alarm) args.string("time", 128)!! else {
            requireBridge(!args.json.has("time"), "Timer countdown/end time is not identity")
            requireBridge(label.isNotEmpty(), "Named timer selection is required")
            ""
        }
        val target = GoogleClockCollection.select(before.values, label, time)
        val desired = when (op) {
            "clock.alarms.enable" -> "enabled"
            "clock.alarms.disable" -> "disabled"
            "clock.alarms.delete", "clock.timers.cancel" -> null
            "clock.timers.pause" -> "paused"
            "clock.timers.resume" -> "running"
            else -> failure("UNSUPPORTED", "Unknown Google Clock mutation")
        }
        val expected = GoogleClockCollection.expected(before.values, target, desired)
        if (desired == target.state) {
            val fresh = snapshot(service, alarm)
            requireSchema(fresh.window == before.window && GoogleClockCollection.same(fresh.values, before.values), "Clock changed before no-op readback")
            return JSONObject().put("outcome", "unchanged").put("verified", true).put("scope", "complete_current_collection")
        }
        if (alarm && desired == null) openDetail(service, before, target)
        var dispatched = false
        var effectWindow = before.window
        try {
            gate()
            UiThread.call {
                val node = if (alarm && desired == null) detail(service, before, target) else {
                    val fresh = collection(service, alarm)
                    requireSchema(fresh.window == before.window && GoogleClockCollection.same(fresh.values, before.values), "Clock changed before effect dispatch")
                    val row = fresh.rows.single { it.value.key == target.key }
                    if (alarm) requireSchema(row.control.isCheckable && row.control.isChecked == (target.state == "enabled"), "Clock checked state changed before click")
                    if (desired == null) row.deletion ?: failure("UNSUPPORTED", "Clock same-row deletion missing") else row.control
                }
                clickable(node)
                Access.unlocked(service)
                effectWindow = node.windowId
                dispatched = true
                service.click(node)
            }
            val deadline = SystemClock.elapsedRealtime() + 4000
            Thread.sleep(100)
            if (alarm && desired == null) awaitDetailClose(service, target, before.window, effectWindow, deadline)
            val readback = awaitSnapshot(service, alarm, expected, deadline, before.window)
            rebind(service, alarm, expected, readback.window, deadline)
            return JSONObject().put("outcome", if (desired == null) "deleted" else "changed").put("verified", true).put("scope", "complete_collection_model_rebind").put("state", desired ?: "absent").put("label", label).put("time", if (alarm) time else JSONObject.NULL).put("vendor_id", JSONObject.NULL)
        } catch (e: Exception) {
            if (dispatched) failure("AMBIGUOUS_STATE", "Clock effect dispatched but complete model-rebind readback failed; never retry effects")
            throw e
        }
    }
}
