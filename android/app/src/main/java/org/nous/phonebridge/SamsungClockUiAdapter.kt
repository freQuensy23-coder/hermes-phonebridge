package org.nous.phonebridge

import android.graphics.Rect
import android.graphics.RectF
import android.os.Bundle
import android.os.SystemClock
import android.view.accessibility.AccessibilityNodeInfo
import android.view.accessibility.AccessibilityWindowInfo
import org.json.JSONArray
import org.json.JSONObject

internal class SamsungClockUiAdapter : ClockUiAdapter {
    override val name = "samsung_clock_12_5_07_13_source_candidate"
    override fun supported(version: String, code: Long): Set<String> =
        if (version == SamsungClockProfile.VERSION && code == SamsungClockProfile.CODE && BridgeAccessibility.current?.let { SamsungClockProfile.installed(it) } == true) SamsungClockProfile.operations else emptySet()
    override fun reason(version: String, code: Long) = "Source-bound candidate for exact Samsung Clock ${SamsungClockProfile.VERSION}/${SamsungClockProfile.CODE} APK and current signer; Samsung API36, English 24-hour, complete single-column UI only; installed=$version/$code; CI/physical proof and durable scheduling unverified; all other releases UNSUPPORTED"

    private data class Row(val value: SamsungClockEntry, val card: AccessibilityNodeInfo, val action: AccessibilityNodeInfo, val deletion: AccessibilityNodeInfo?, val index: Int)
    private data class Collection(val rows: List<Row>, val window: Int) {
        val values get() = rows.map { it.value }
        val order get() = rows.sortedBy { it.index }.map { it.value }
    }
    private data class Snapshot(val values: List<SamsungClockEntry>, val order: List<SamsungClockEntry>, val window: Int)
    private fun id(value: String) = "${SamsungClockProfile.PACKAGE}:id/$value"
    private fun schema(value: Boolean, message: String) = SamsungClockPolicy.requireSchema(value, message)
    private fun check(deadline: Long) {
        ExecutionGuard.check()
        if (SystemClock.elapsedRealtime() >= deadline) failure("UNSUPPORTED", "Samsung UI execution deadline exceeded before further action")
    }
    private fun nodes(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val result = ArrayList<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            schema(depth <= 30 && result.size < 400 && node.refresh() && node.childCount <= 100, "Samsung tree is incomplete or exceeds traversal bound")
            schema(!node.isPassword && node.packageName?.toString() == SamsungClockProfile.PACKAGE, "Samsung tree contains a password or foreign node")
            result.add(node)
            for (index in 0 until node.childCount) visit(node.getChild(index) ?: failure("UNSUPPORTED", "Samsung tree child unavailable"), depth + 1)
        }
        visit(root, 0)
        return result
    }
    private fun visible(nodes: List<AccessibilityNodeInfo>, resource: String) = nodes.filter { it.viewIdResourceName == id(resource) && it.isVisibleToUser }
    private fun one(nodes: List<AccessibilityNodeInfo>, resource: String) = visible(nodes, resource).singleOrNull() ?: failure("UNSUPPORTED", "Missing or duplicate source Samsung $resource")
    private fun text(node: AccessibilityNodeInfo, description: Boolean = false): String = SamsungClockPolicy.text((if (description) node.contentDescription else node.text)?.toString() ?: failure("UNSUPPORTED", "Samsung identity text is missing"))
    private fun bounds(node: AccessibilityNodeInfo) = Rect().also { node.getBoundsInScreen(it) }
    private fun contained(node: AccessibilityNodeInfo, parent: AccessibilityNodeInfo) {
        schema(!bounds(node).isEmpty && bounds(parent).contains(bounds(node)), "Samsung identity/control/row is clipped")
    }
    private fun fullText(node: AccessibilityNodeInfo, card: AccessibilityNodeInfo): String {
        contained(node, card)
        val value = text(node)
        val extra = AccessibilityNodeInfo.EXTRA_DATA_TEXT_CHARACTER_LOCATION_KEY
        schema(extra in node.availableExtraData, "Samsung ellipsized identity has no full character-coverage evidence")
        val accepted = node.refreshWithExtraData(extra, Bundle().apply {
            putInt(AccessibilityNodeInfo.EXTRA_DATA_TEXT_CHARACTER_LOCATION_ARG_START_INDEX, 0)
            putInt(AccessibilityNodeInfo.EXTRA_DATA_TEXT_CHARACTER_LOCATION_ARG_LENGTH, value.length)
        })
        schema(accepted && node.text?.toString() == value, "Samsung identity changed during full-text acquisition")
        val locations = node.extras.getParcelableArray(extra) ?: failure("UNSUPPORTED", "Samsung character locations unavailable")
        val area = RectF(bounds(node))
        schema(locations.size == value.length && locations.all { it is RectF && !it.isEmpty && area.contains(it) }, "Samsung identity is ellipsized or incompletely exposed")
        return value
    }
    private fun clickable(node: AccessibilityNodeInfo, parent: AccessibilityNodeInfo) {
        schema(node.isVisibleToUser && node.isEnabled && node.isClickable && !node.isPassword, "Samsung source control is not safely clickable")
        contained(node, parent)
    }
    private fun windows(service: BridgeAccessibility, popup: Boolean): AccessibilityNodeInfo {
        val root = service.safeRoot(SamsungClockProfile.PACKAGE)
        val windows = service.windows
        val apps = windows.filter { it.type == AccessibilityWindowInfo.TYPE_APPLICATION }
        schema(apps.size == if (popup) 2 else 1, "Samsung window graph is unknown or covered")
        schema(apps.count { it.id == root.windowId && it.isActive && it.isFocused } == 1, "Samsung requires one focused active window")
        schema(windows.filter { it.type != AccessibilityWindowInfo.TYPE_APPLICATION }.all { it.type == AccessibilityWindowInfo.TYPE_SYSTEM && !it.isActive && !it.isFocused && it.root?.packageName?.toString() == "com.android.systemui" }, "Samsung unknown overlay/input window")
        apps.forEach { nodes(it.root ?: failure("UNSUPPORTED", "Samsung application window unavailable")) }
        return root
    }
    private fun card(anchor: AccessibilityNodeInfo, recycler: AccessibilityNodeInfo, alarm: Boolean): AccessibilityNodeInfo {
        var node = anchor
        repeat(30) {
            schema(node != recycler, "Samsung source row ancestry unavailable")
            if (node.viewIdResourceName == id(if (alarm) "alarm_list_cardView" else "timer_list_Cardview")) return node
            node = node.parent ?: failure("UNSUPPORTED", "Samsung source card parent missing")
        }
        failure("UNSUPPORTED", "Samsung source card ancestry exceeds bound")
    }
    private fun index(card: AccessibilityNodeInfo, recycler: AccessibilityNodeInfo): Int {
        var node = card
        repeat(30) {
            schema(node != recycler, "Samsung independent row metadata unavailable")
            node.collectionItemInfo?.let {
                schema(it.rowSpan == 1 && it.columnSpan == 1 && it.columnIndex == 0 && !it.isHeading && !it.isSelected, "Samsung grid/group/selected collection is excluded")
                return it.rowIndex
            }
            node = node.parent ?: failure("UNSUPPORTED", "Samsung item metadata ancestry unavailable")
        }
        failure("UNSUPPORTED", "Samsung item metadata exceeds bound")
    }
    private fun collection(service: BridgeAccessibility, alarm: Boolean, root: AccessibilityNodeInfo = windows(service, false)): Collection {
        val all = nodes(root)
        val excluded = setOf("alarm_turn_on_off_progress", "alarm_pre_dismiss_button_layout", "on_off_text_routine", "alarmListCheckBox", "timerListCheckBox", "iv_reorder_icon", "group_alarm_on_off_switch", "bedtime_onoff_switch", "timer_pause_button", "timer_resume_button", "timer_cancel_button", "timer_restart_button", "timer_dismiss_button", "timer_reset_button", "timer_carousel_scroll_helper")
        schema(all.none { it.isVisibleToUser && (it.viewIdResourceName in excluded.map(::id) || it.viewIdResourceName?.startsWith(id("group_")) == true) }, "Samsung selection, group, bedtime, carousel or transition mode is excluded")
        if (alarm) one(all, "alarm_main_layout")
        val recycler = all.singleOrNull { it.viewIdResourceName == id(if (alarm) "alarm_list" else "multi_timer_list") } ?: failure("UNSUPPORTED", "Source Samsung collection anchor/count is not exposed")
        val metadata = recycler.collectionInfo ?: failure("UNSUPPORTED", "Samsung full model collection count unavailable")
        val count = SamsungClockPolicy.count(metadata.rowCount, metadata.columnCount)
        schema(!metadata.isHierarchical && metadata.selectionMode == AccessibilityNodeInfo.CollectionInfo.SELECTION_MODE_NONE, "Samsung hierarchical/selection collection is excluded")
        schema(count == 0 || recycler.isVisibleToUser, "Samsung populated collection is hidden")
        val scroll = setOf(AccessibilityNodeInfo.ACTION_SCROLL_FORWARD, AccessibilityNodeInfo.ACTION_SCROLL_BACKWARD, android.R.id.accessibilityActionScrollUp, android.R.id.accessibilityActionScrollDown, android.R.id.accessibilityActionScrollLeft, android.R.id.accessibilityActionScrollRight)
        schema(!recycler.isScrollable && recycler.actionList.none { it.id in scroll }, "Samsung offscreen/scrollable collections cannot prove complete scope")
        val children = nodes(recycler)
        val anchors = visible(children, if (alarm) "alarm_onoff_switch" else "timer_list_timer_name")
        val rows = anchors.map { anchor ->
            val card = card(anchor, recycler, alarm)
            contained(card, recycler)
            contained(card, root)
            val rowNodes = nodes(card)
            val item = one(rowNodes, if (alarm) "alarm_list_item" else "timer_list")
            schema(!item.isSelected && !item.isChecked, "Samsung action/selection mode is active")
            val position = index(card, recycler)
            if (alarm) {
                schema(one(rowNodes, "alarm_onoff_switch") == anchor && anchor.isCheckable && anchor.className?.toString() == "android.widget.Switch", "Unknown Samsung enabled-switch binding")
                clickable(anchor, card)
                schema(visible(rowNodes, "alarm_item_ampm").isEmpty() && visible(rowNodes, "alarm_list_holiday_or_pre_dismiss_alarm_text_view").isEmpty(), "Samsung 12-hour/holiday/predismiss alarm is excluded")
                val label = fullText(one(rowNodes, "alarm_list_alarm_name"), card)
                val clock = one(rowNodes, "alarm_item_time")
                val time = fullText(clock, card)
                schema(time.matches(Regex("(?:[01]?[0-9]|2[0-3]):[0-5][0-9]")), "Samsung current 24-hour time is unknown")
                val spokenTime = text(clock, true)
                val switchDescription = text(anchor, true)
                val itemDescription = text(item, true)
                val schedule = SamsungClockPolicy.alarmSchedule(label, spokenTime, switchDescription, itemDescription)
                Row(SamsungClockEntry(label, time, if (anchor.isChecked) "enabled" else "disabled", schedule), card, anchor, null, position)
            } else {
                schema(one(rowNodes, "timer_list_timer_name") == anchor, "Samsung timer row mapping is unknown")
                val label = fullText(anchor, card)
                val action = one(rowNodes, "list_action_btn")
                val deletion = one(rowNodes, "cancel_btn")
                clickable(action, card)
                clickable(deletion, card)
                schema(text(deletion, true) == "Delete", "Samsung timer cancellation is not source Delete")
                val state = SamsungClockPolicy.timerState(text(action, true))
                SamsungClockPolicy.remaining(text(one(rowNodes, "timer_time")))
                one(rowNodes, "timer_list_end_time")
                Row(SamsungClockEntry(label, "", state), card, action, deletion, position)
            }
        }
        schema(rows.map { it.card }.distinct().size == rows.size, "Samsung rows overlap")
        schema(visible(children, if (alarm) "alarm_list_cardView" else "timer_list_Cardview").size == rows.size, "Samsung collection contains unknown cards")
        if (alarm) {
            if (count == 0) schema(text(one(all, "no_alarm_text")) == "No alarms", "Samsung source empty alarm state unavailable")
            else schema(visible(all, "no_alarm_text").isEmpty(), "Samsung empty/list states overlap")
        } else if (count == 0) {
            one(all, "timer_picker_view")
            val start = one(all, "timer_start_button")
            schema(text(start) == "Start" && text(start, true) == "Start" && start.isEnabled, "Samsung zero-model timer setup is not source-corroborated")
        }
        SamsungClockPolicy.complete(rows.map { it.value }, count, rows.map { it.index })
        return Collection(rows, root.windowId)
    }
    private fun snapshot(service: BridgeAccessibility, alarm: Boolean, binding: SamsungClockProfile.Binding, deadline: Long): Snapshot {
        check(deadline)
        schema(SamsungClockProfile.binding(service) == binding, "Samsung installed profile changed")
        val current = UiThread.call {
            check(deadline)
            val collection = collection(service, alarm)
            Snapshot(collection.values, collection.order, collection.window)
        }
        check(deadline)
        return current
    }
    private fun menu(service: BridgeAccessibility, before: Snapshot, target: SamsungClockEntry): AccessibilityNodeInfo {
        val root = windows(service, true)
        schema(root.windowId != before.window, "Samsung selected context menu is not isolated")
        val underlying = service.windows.singleOrNull { it.id == before.window }?.root ?: failure("UNSUPPORTED", "Samsung collection below menu unavailable")
        val current = collection(service, true, underlying)
        schema(current.order == before.order, "Samsung alarm positions changed while context menu was open")
        val all = nodes(root)
        val texts = all.filter { it.isVisibleToUser && !it.text.isNullOrEmpty() }
        SamsungClockPolicy.popupTexts(texts.map { text(it) }, target.menuHeader)
        val header = texts.single { text(it) == target.menuHeader }
        schema(fullText(header, root) == target.menuHeader, "Samsung popup full header is clipped")
        schema(!header.isClickable && !header.isEditable, "Samsung popup header is not a read-only identity")
        var button = texts.single { text(it) == "Delete" }
        repeat(30) {
            if (button.isClickable) {
                clickable(button, root)
                val descendants = nodes(button)
                schema(descendants.filter { it.isVisibleToUser && !it.text.isNullOrEmpty() }.singleOrNull()?.text?.toString() == "Delete", "Samsung popup action contains another identity")
                schema(all.filter { it.isVisibleToUser && (it.isClickable || it.isLongClickable || it.isEditable) }.all { it == button || descendants.contains(it) }, "Samsung popup contains unrelated actions")
                return button
            }
            schema(button != root, "Samsung context menu action is not accessible")
            button = button.parent ?: failure("UNSUPPORTED", "Samsung menu action ancestry missing")
        }
        failure("UNSUPPORTED", "Samsung menu action ancestry exceeds bound")
    }
    private fun openMenu(service: BridgeAccessibility, before: Snapshot, target: SamsungClockEntry, binding: SamsungClockProfile.Binding, deadline: Long) {
        schema(SamsungClockProfile.requireInstalled(service) == binding, "Samsung APK changed before context menu")
        val permitted = ExecutionGuard.snapshot()
        UiThread.call {
            check(deadline)
            val current = collection(service, true)
            schema(current.window == before.window && current.order == before.order, "Samsung alarm selection changed before long-click")
            val card = current.rows.single { it.value.key == target.key }.card
            schema(card.isVisibleToUser && card.isEnabled && card.isLongClickable && !card.isPassword, "Samsung source card has no accessible context menu action")
            Access.unlocked(service)
            check(deadline)
            permitted()
            schema(card.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK), "Samsung context menu long-click refused")
        }
        repeat(20) {
            check(deadline)
            val loaded = UiThread.call {
                val root = service.safeRoot(SamsungClockProfile.PACKAGE)
                root.windowId != before.window
            }
            if (loaded) { UiThread.call { check(deadline); menu(service, before, target) }; return }
            Thread.sleep(100)
        }
        failure("UNSUPPORTED", "Samsung full selected context menu was not exposed; no deletion dispatched")
    }
    private fun readback(service: BridgeAccessibility, alarm: Boolean, expected: List<SamsungClockEntry>, before: Snapshot, binding: SamsungClockProfile.Binding, deadline: Long, popup: Boolean) {
        var matchedAt: Long? = null
        repeat(20) {
            check(deadline)
            if (popup && UiThread.call { service.safeRoot(SamsungClockProfile.PACKAGE).windowId != before.window }) {
                UiThread.call { check(deadline); windows(service, true) }
                matchedAt = null
            } else {
                val current = snapshot(service, alarm, binding, deadline)
                schema(current.window == before.window, "Samsung window changed during effect readback")
                if (SamsungClockPolicy.same(current.values, expected)) {
                    val now = SystemClock.elapsedRealtime()
                    if (matchedAt == null) matchedAt = now
                    if (now - matchedAt!! >= 300) return
                } else matchedAt = null
            }
            Thread.sleep(100)
        }
        failure("AMBIGUOUS_STATE", "Samsung complete stable collection could not prove effect; no retry")
    }
    override fun execute(service: BridgeAccessibility, op: String, args: Arguments): JSONObject {
        val deadline = SystemClock.elapsedRealtime() + 8000
        check(deadline)
        Access.unlocked(service)
        val binding = SamsungClockProfile.requireInstalled(service)
        schema(op in SamsungClockProfile.operations, "Unknown Samsung Clock UI operation")
        args.only("package", "id", "label", "time", "state")
        requireBridge(args.string("package", 256) == SamsungClockProfile.PACKAGE, "Samsung package is required")
        if (args.json.has("id")) failure("UNSUPPORTED", "Samsung source exposes no stable vendor IDs; use exact current label/time/state")
        val alarm = op.startsWith("clock.alarms.")
        val before = snapshot(service, alarm, binding, deadline)
        if (op.endsWith(".list")) {
            requireBridge(listOf("label", "time", "state").none { args.json.has(it) }, "Samsung list does not accept selection fields")
            return JSONObject().put(if (alarm) "alarms" else "timers", JSONArray(before.values.map { value ->
                JSONObject().put("id", JSONObject.NULL).put("label", value.label).put("time", if (alarm) value.time else JSONObject.NULL).put("state", value.state).put("schedule", value.schedule).put("identity_scope", "current_unique_display_selection").apply { if (alarm) put("enabled", value.state == "enabled") }
            })).put("complete", true).put("vendor_ids_available", false).put("scope", "complete_current_collection")
        }
        val label = SamsungClockPolicy.text(args.string("label", 256)!!)
        val time = if (alarm) args.string("time", 128)!! else {
            requireBridge(!args.json.has("time"), "Samsung countdown/end time is not timer identity")
            ""
        }
        val state = args.string("state", 16, !alarm)
        if (!alarm) requireBridge(state in setOf("running", "paused"), "Samsung active timer expected state is required")
        val target = SamsungClockPolicy.select(before.values, label, time, state)
        val desired = when (op) {
            "clock.alarms.enable" -> "enabled"
            "clock.alarms.disable" -> "disabled"
            "clock.timers.pause" -> "paused"
            "clock.timers.resume" -> "running"
            "clock.alarms.delete", "clock.timers.cancel" -> null
            else -> failure("UNSUPPORTED", "Unknown Samsung effect")
        }
        val expected = SamsungClockPolicy.expected(before.values, target, desired)
        if (desired == target.state) {
            val fresh = snapshot(service, alarm, binding, deadline)
            schema(fresh.window == before.window && SamsungClockPolicy.same(fresh.values, before.values), "Samsung collection changed before no-op readback")
            return JSONObject().put("outcome", "unchanged").put("verified", true).put("scope", "complete_current_collection").put("durable_scheduling_verified", false)
        }
        val popup = alarm && desired == null
        if (popup) openMenu(service, before, target, binding, deadline)
        var dispatched = false
        try {
            schema(SamsungClockProfile.requireInstalled(service) == binding, "Samsung APK changed before effect")
            val permitted = ExecutionGuard.snapshot()
            UiThread.call {
                check(deadline)
                val control = if (popup) menu(service, before, target) else {
                    val current = collection(service, alarm)
                    schema(current.window == before.window && SamsungClockPolicy.same(current.values, before.values), "Samsung target/survivors changed before effect")
                    val row = current.rows.single { it.value.key == target.key }
                    if (desired == null) row.deletion ?: failure("UNSUPPORTED", "Samsung same-row deletion unavailable") else row.action
                }
                clickable(control, service.safeRoot(SamsungClockProfile.PACKAGE))
                Access.unlocked(service)
                check(deadline)
                permitted()
                dispatched = true
                service.click(control)
            }
            val readbackDeadline = minOf(deadline, SystemClock.elapsedRealtime() + 2500)
            Thread.sleep(300)
            readback(service, alarm, expected, before, binding, readbackDeadline, popup)
            check(readbackDeadline)
            Access.unlocked(service)
            return JSONObject().put("outcome", if (desired == null) "deleted" else "changed").put("verified", true).put("scope", "complete_collection_stable_ui_readback").put("state", desired ?: "absent").put("label", label).put("time", if (alarm) time else JSONObject.NULL).put("vendor_id", JSONObject.NULL).put("durable_scheduling_verified", false)
        } catch (e: Exception) {
            if (dispatched) failure("AMBIGUOUS_STATE", "Samsung effect was dispatched but fresh complete readback failed; no automatic retry")
            throw e
        }
    }
}
