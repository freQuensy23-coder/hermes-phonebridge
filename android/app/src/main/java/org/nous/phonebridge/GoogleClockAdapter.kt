package org.nous.phonebridge

import android.content.ComponentName
import android.content.Context
import android.content.Intent
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
            requireSchema(current.windowId == node.windowId, "Clock tree contains stale-window nodes")
            result.add(current)
            for (index in 0 until current.childCount) visit(current.getChild(index) ?: failure("UNSUPPORTED", "Clock tree child unavailable"), depth + 1)
        }
        visit(node, 0)
        return result
    }

    private fun windowState(windows: List<AccessibilityWindowInfo>) = windows.sortedBy { it.id }.map { window ->
        val bounds = Rect().also { window.getBoundsInScreen(it) }
        listOf(window.id, window.type, window.isActive, window.isFocused, window.layer, window.root?.packageName?.toString(), GoogleClockBounds(bounds.left, bounds.top, bounds.right, bounds.bottom))
    }
    private fun activeRoot(service: BridgeAccessibility): AccessibilityNodeInfo {
        val root = service.safeRoot(GoogleClockProfile.PACKAGE)
        val windows = service.windows
        val graph = windowState(windows)
        val application = windows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        requireSchema(windows.size <= 8 && application.isNotEmpty(), "Clock window graph exceeds profile bounds")
        val trees = application.associate { window ->
            val windowRoot = window.root ?: failure("UNSUPPORTED", "Clock window root unavailable")
            requireSchema(windowRoot.windowId == window.id, "Clock window root identity changed")
            window.id to completeNodes(windowRoot)
        }
        trees.values.forEach { nodes ->
            requireSchema(visible(nodes, "body").isEmpty() && visible(nodes, "dismiss_button").isEmpty(), "Clock promotion popup is not a collection host")
        }
        val navigationBackgrounds = trees.values.flatten().filter { it.viewIdResourceName == "android:id/navigationBarBackground" }.map { node ->
            Rect().also { node.getBoundsInScreen(it) }.let { GoogleClockBounds(it.left, it.top, it.right, it.bottom) }
        }.distinct()
        val safeSystem = windows.filter { it.type != AccessibilityWindowInfo.TYPE_APPLICATION }.all { window ->
            val bounds = Rect().also { window.getBoundsInScreen(it) }
            GoogleClockWindows.system(android.os.Build.VERSION.SDK_INT, window.type, window.isActive, window.isFocused, window.root?.packageName?.toString(), GoogleClockBounds(bounds.left, bounds.top, bounds.right, bounds.bottom), navigationBackgrounds)
        }
        GoogleClockModalGraph.requireSingle(application.map { GoogleClockApplication(it.id, trees.getValue(it.id).first().packageName?.toString(), it.isActive, it.isFocused, trees.getValue(it.id).first().isVisibleToUser) }, root.windowId, windows.count { it.isActive }, windows.count { it.isFocused }, safeSystem)
        completeNodes(root)
        val returnedWindows = service.windows
        if (windowState(returnedWindows) != graph) {
            requireSchema(returnedWindows.size <= 8, "Clock changed window graph exceeds profile bounds")
            returnedWindows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }.forEach { window ->
                val nodes = completeNodes(window.root ?: failure("UNSUPPORTED", "Changed Clock window root unavailable"))
                requireSchema(visible(nodes, "body").isEmpty() && visible(nodes, "dismiss_button").isEmpty(), "Clock promotion popup is not a collection host")
            }
            failure("UNSUPPORTED", "Clock window safety graph changed during fresh traversal")
        }
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
        val stable = GoogleClockReadback(expected, expectedWindow)
        while (SystemClock.elapsedRealtime() < deadline) {
            val current = snapshot(service, alarm)
            val now = SystemClock.elapsedRealtime()
            if (now >= deadline) failure("AMBIGUOUS_STATE", "Clock readback deadline exceeded; no effect retry")
            if (stable.observe(current.values, current.window, now)) return current
            Thread.sleep(100)
        }
        failure("AMBIGUOUS_STATE", "Clock complete readback could not prove the effect; no retry")
    }
    private fun rebind(service: BridgeAccessibility, alarm: Boolean, expected: List<GoogleClockEntry>, window: Int, transaction: GoogleClockAlarmDelete? = null, permitted: () -> Unit = ExecutionGuard.snapshot()) {
        ExecutionGuard.check()
        permitted()
        transaction?.check(SystemClock.elapsedRealtime())
        gate()
        val component = ComponentName(GoogleClockProfile.PACKAGE, GoogleClockProfile.ACTIVITY)
        val activity = context!!.packageManager.getActivityInfo(component, 0)
        requireSchema(activity.exported && activity.enabled && activity.packageName == GoogleClockProfile.PACKAGE && activity.name == GoogleClockProfile.ACTIVITY, "Clock model-rebind activity does not match the signed profile")
        val intent = Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER).setPackage(GoogleClockProfile.PACKAGE).setComponent(component).setFlags(GoogleClockReadback.RECREATE_FLAGS)
        UiThread.call {
            permitted()
            transaction?.check(SystemClock.elapsedRealtime())
            gate()
            val fresh = collection(service, alarm)
            requireSchema(fresh.window == window && GoogleClockCollection.same(fresh.values, expected), "Clock changed before model rebind")
            Access.unlocked(service)
            permitted()
            transaction?.recreate(SystemClock.elapsedRealtime())
            service.startActivity(intent)
        }
        val deadline = SystemClock.elapsedRealtime() + GoogleClockReadback.TIMEOUT_MS
        var returnedWindow: Int? = null
        while (returnedWindow == null && SystemClock.elapsedRealtime() < deadline) {
            ExecutionGuard.check()
            permitted()
            transaction?.check(SystemClock.elapsedRealtime())
            gate()
            returnedWindow = UiThread.call {
                permitted()
                transaction?.check(SystemClock.elapsedRealtime())
                Access.unlocked(service)
                val root = service.rootInActiveWindow ?: return@call null
                requireSchema(root.packageName?.toString() == GoogleClockProfile.PACKAGE, "Clock foreground changed during model rebind")
                val windows = service.windows
                val applications = windows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
                val roots = applications.map { it.root ?: return@call null }
                requireSchema(roots.all { it.packageName?.toString() == GoogleClockProfile.PACKAGE }, "Clock model rebind contains a foreign application")
                if (root.windowId == window || windows.any { it.id == window }) return@call null
                if (applications.none { it.id == root.windowId && it.isActive && it.isFocused }) return@call null
                GoogleClockReadback.requireRecreated(window, root.windowId, windows.any { it.id == window })
                val nodes = completeNodes(activeRoot(service))
                val resource = if (alarm) "alarm_recycler_view" else "timer_view"
                if (visible(nodes, "loading_indicator").isNotEmpty() || visible(nodes, resource).size != 1) return@call null
                root.windowId
            }
            if (returnedWindow == null) Thread.sleep(100)
        }
        val rebound = returnedWindow ?: failure("UNSUPPORTED", "Clock did not recreate its model-backed collection window")
        if (transaction == null) awaitSnapshot(service, alarm, expected, SystemClock.elapsedRealtime() + GoogleClockReadback.TIMEOUT_MS, rebound)
        else while (true) {
            ExecutionGuard.check()
            permitted()
            transaction.check(SystemClock.elapsedRealtime())
            gate()
            val complete = UiThread.call {
                permitted()
                Access.unlocked(service)
                val fresh = collection(service, alarm)
                requireSchema(fresh.window == rebound, "Clock recreated window changed before native model proof")
                transaction.complete(fresh.values, fresh.window, service.windows.any { it.id == window }, service.windows.any { it.id == transaction.detailWindow }, SystemClock.elapsedRealtime())
            }
            if (complete) {
                permitted()
                Access.unlocked(service)
                break
            }
            Thread.sleep(100)
        }
    }
    private fun detailDays(recurrence: AccessibilityNodeInfo): List<Pair<String, Boolean>> {
        requireSchema(recurrence.className?.toString() == "android.view.ViewGroup", "Unknown Clock detail recurrence schema")
        val dayNodes = completeNodes(recurrence)
        val days = (0..6).map { index ->
            val day = one(dayNodes, "day_button_$index")
            requireSchema(day.className?.toString() == "android.widget.CheckBox" && day.isCheckable && day.isEnabled && day.parent == recurrence, "Unknown Clock weekday checkbox schema")
            contained(day, recurrence)
            boundedText(day, true) to day.isChecked
        }
        requireSchema(dayNodes.count { it.isCheckable && it.isVisibleToUser } == 7, "Clock recurrence contains unknown controls")
        return days
    }
    private fun detailSurface(nodes: List<AccessibilityNodeInfo>, window: Int) {
        requireSchema(nodes.none { it.isEditable || it.className?.toString() == "android.widget.EditText" || it.windowId != window }, "Clock detail contains input or stale-window nodes")
        requireSchema(nodes.filter { it.isVisibleToUser }.all { node -> node.viewIdResourceName?.let { it.startsWith("${GoogleClockProfile.PACKAGE}:id/") || it in setOf("android:id/content", "android:id/navigationBarBackground", "android:id/statusBarBackground") } != false }, "Clock detail contains unknown resource namespaces")
        GoogleClockAlarmDetail.requireSurface(nodes.filter { it.isVisibleToUser }.mapNotNull { it.viewIdResourceName?.takeIf { resource -> resource.startsWith("${GoogleClockProfile.PACKAGE}:id/") }?.substringAfter(":id/") })
    }
    private fun detail(service: BridgeAccessibility, transaction: GoogleClockAlarmDelete, root: AccessibilityNodeInfo = activeRoot(service)): Pair<AccessibilityNodeInfo, Boolean> {
        val graph = windowState(service.windows)
        val nodes = completeNodes(root)
        detailSurface(nodes, root.windowId)
        val clock = one(nodes, "clock")
        val label = one(nodes, "alarm_label")
        requireSchema(clock.className?.toString() == "android.widget.TextView" && label.className?.toString() == "android.widget.TextView", "Unknown Clock detail identity schema")
        val recurrence = one(nodes, "repeat_days")
        val days = detailDays(recurrence)
        val save = one(nodes, "save_button")
        val deletion = one(nodes, "delete_button")
        requireSchema(save.className?.toString() == "android.widget.Button" && boundedText(save) == "Save" && deletion.className?.toString() == "android.widget.Button" && boundedText(deletion) == "Delete", "Unknown Clock detail action schema")
        clickable(save)
        clickable(deletion)
        requireSchema(deletion.actionList.any { it.id == AccessibilityNodeInfo.ACTION_CLICK }, "Clock Delete has no source-bound native click")
        contained(deletion, root)
        contained(clock, root)
        contained(label, root)
        val finalRoot = activeRoot(service)
        requireSchema(finalRoot.windowId == root.windowId, "Clock detail window changed during identity readback")
        val finalNodes = completeNodes(finalRoot)
        detailSurface(finalNodes, finalRoot.windowId)
        requireSchema(one(finalNodes, "clock") == clock && one(finalNodes, "alarm_label") == label && one(finalNodes, "repeat_days") == recurrence && one(finalNodes, "delete_button") == deletion, "Clock selected detail controls were replaced during identity readback")
        requireSchema(clock.refresh() && label.refresh() && clock.isVisibleToUser && label.isVisibleToUser && clock.className?.toString() == "android.widget.TextView" && label.className?.toString() == "android.widget.TextView" && detailDays(recurrence) == days, "Clock detail identity or recurrence changed during fresh readback")
        requireSchema(deletion.refresh() && deletion.windowId == root.windowId && deletion.viewIdResourceName == id("delete_button") && deletion.className?.toString() == "android.widget.Button" && boundedText(deletion) == "Delete", "Clock Delete changed after fresh modal readback")
        clickable(deletion)
        contained(deletion, root)
        if (windowState(service.windows) != graph) {
            activeRoot(service)
            failure("UNSUPPORTED", "Clock modal safety graph changed during identity readback")
        }
        val stable = transaction.detail(root.windowId, boundedText(label, empty = true), boundedText(clock), boundedText(clock, true), days, SystemClock.elapsedRealtime())
        return deletion to stable
    }

    private fun deleteAlarm(service: BridgeAccessibility, before: Snapshot, target: GoogleClockEntry): JSONObject {
        val transaction = GoogleClockAlarmDelete(before.values.toList(), before.window, target, SystemClock.elapsedRealtime())
        val permitted = ExecutionGuard.snapshot()
        fun check() {
            ExecutionGuard.check()
            permitted()
            transaction.check(SystemClock.elapsedRealtime())
            Access.unlocked(service)
        }
        while (true) {
            check()
            val current = snapshot(service, true)
            if (transaction.baseline(current.values, current.window, SystemClock.elapsedRealtime())) break
            Thread.sleep(100)
        }
        UiThread.call {
            check()
            gate()
            val fresh = collection(service, true)
            val row = fresh.rows.singleOrNull { it.value == target } ?: failure("UNSUPPORTED", "Clock selected card identity changed")
            clickable(row.node)
            requireSchema(row.node.actionList.any { it.id == AccessibilityNodeInfo.ACTION_CLICK }, "Clock selected card has no native detail action")
            transaction.select(fresh.values, fresh.window, SystemClock.elapsedRealtime())
            permitted()
            Access.unlocked(service)
            service.click(row.node)
        }
        val openingDeadline = SystemClock.elapsedRealtime() + 2000
        while (true) {
            check()
            requireSchema(SystemClock.elapsedRealtime() < openingDeadline, "Clock selected detail did not settle before mutation")
            val stable = UiThread.call {
                check()
                val root = try {
                    activeRoot(service)
                } catch (error: BridgeFailure) {
                    if (transaction.phase != GoogleClockAlarmDelete.Phase.SELECTED || error.code != "UNSUPPORTED" || error.message != "Clock window root unavailable") throw error
                    null
                }
                if (root == null) {
                    false
                } else if (root.windowId == before.window) {
                    requireSchema(transaction.phase == GoogleClockAlarmDelete.Phase.SELECTED && GoogleClockCollection.same(collection(service, true, root).values, before.values), "Clock selection returned to a changed collection")
                    false
                } else detail(service, transaction, root).second
            }
            if (stable) break
            Thread.sleep(100)
        }
        var dispatched = false
        var phase = "single native Delete dispatch"
        try {
            UiThread.call {
                check()
                gate()
                val (deletion, stable) = detail(service, transaction)
                requireSchema(stable, "Clock selected detail is not stable before Delete")
                Access.unlocked(service)
                permitted()
                transaction.dispatch(deletion.windowId, SystemClock.elapsedRealtime())
                dispatched = true
                service.click(deletion)
            }
            phase = "complete returned collection proof"
            while (true) {
                check()
                gate()
                val returned = UiThread.call {
                    check()
                    val root = activeRoot(service)
                    if (root.windowId == transaction.detailWindow) {
                        detail(service, transaction, root)
                        false
                    } else {
                        val fresh = collection(service, true, root)
                        transaction.returned(fresh.values, fresh.window, service.windows.any { it.id == transaction.detailWindow }, SystemClock.elapsedRealtime())
                    }
                }
                if (returned) break
                Thread.sleep(100)
            }
            phase = "native model recreation and complete readback"
            rebind(service, true, transaction.expected, before.window, transaction, permitted)
            requireSchema(transaction.phase == GoogleClockAlarmDelete.Phase.COMPLETE, "Clock deletion lacks complete native model proof")
            return JSONObject().put("outcome", "deleted").put("verified", true).put("scope", "complete_collection_model_rebind").put("state", "absent").put("label", target.label).put("time", target.time).put("vendor_id", JSONObject.NULL)
        } catch (e: Exception) {
            if (dispatched) failure("AMBIGUOUS_STATE", "Clock Delete dispatched but $phase failed: ${(e as? BridgeFailure)?.code ?: e.javaClass.simpleName}: ${e.message?.take(256)}; never retry effects")
            throw e
        }
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
        if (alarm && desired == null) return deleteAlarm(service, before, target)
        var dispatched = false
        var phase = "effect dispatch"
        try {
            gate()
            UiThread.call {
                val node = run {
                    val fresh = collection(service, alarm)
                    requireSchema(fresh.window == before.window && GoogleClockCollection.same(fresh.values, before.values), "Clock changed before effect dispatch")
                    val row = fresh.rows.single { it.value.key == target.key }
                    if (alarm) requireSchema(row.control.isCheckable && row.control.isChecked == (target.state == "enabled"), "Clock checked state changed before click")
                    if (desired == null) row.deletion ?: failure("UNSUPPORTED", "Clock same-row deletion missing") else row.control
                }
                clickable(node)
                Access.unlocked(service)
                dispatched = true
                service.click(node)
            }
            val deadline = SystemClock.elapsedRealtime() + GoogleClockReadback.TIMEOUT_MS
            Thread.sleep(100)
            phase = "complete collection readback"
            val readback = awaitSnapshot(service, alarm, expected, deadline, before.window)
            phase = "model recreation and readback"
            rebind(service, alarm, expected, readback.window)
            return JSONObject().put("outcome", if (desired == null) "deleted" else "changed").put("verified", true).put("scope", "complete_collection_model_rebind").put("state", desired ?: "absent").put("label", label).put("time", if (alarm) time else JSONObject.NULL).put("vendor_id", JSONObject.NULL)
        } catch (e: Exception) {
            if (dispatched) failure("AMBIGUOUS_STATE", "Clock effect dispatched but $phase failed: ${(e as? BridgeFailure)?.code ?: e.javaClass.simpleName}: ${e.message?.take(256)}; never retry effects")
            throw e
        }
    }
}
