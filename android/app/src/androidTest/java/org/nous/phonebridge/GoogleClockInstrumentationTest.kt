package org.nous.phonebridge

import android.app.KeyguardManager
import android.app.UiAutomation
import android.content.Intent
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestName
import org.junit.runner.RunWith
import java.io.File

@RunWith(AndroidJUnit4::class)
class GoogleClockInstrumentationTest {
    @get:Rule val testName = TestName()
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val pkg = GoogleClockProfile.PACKAGE
    private var optedIn = false
    private var originalServices: String? = null
    private var originalAccessibility = 0
    private var originalTimeFormat: String? = null
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private val evidence = JSONArray()
    private val dismissedCollectionPromotions = ArrayList<String>()
    private val clocks get() = NativeClocks(context)
    private val service get() = BridgeAccessibility.current ?: error("Real PhoneBridge Accessibility service is not connected")

    private fun shell(command: String): String = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).executeShellCommand(command).use { parcel ->
        android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).use { it.readBytes().toString(Charsets.UTF_8) }
    }
    private class VendorFixtureUnavailable(message: String) : RuntimeException(message)
    private fun waitUntil(message: String, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15000
        while (SystemClock.elapsedRealtime() < deadline) {
            val ready = try { predicate() } catch (_: VendorFixtureUnavailable) { false }
            if (ready) return
            Thread.sleep(100)
        }
        fail(message)
    }
    private fun args() = JSONObject().put("package", pkg)
    private fun ui(op: String, selection: JSONObject = args()) = ClockAdapters(context).execute(op, Arguments(selection))
    private var alarmModels = 0
    private var timerModels = 0
    private var defaultAlarms = emptyList<JSONObject>()
    private val alarmHour get() = java.util.Calendar.getInstance().apply { add(java.util.Calendar.HOUR_OF_DAY, 12) }.get(java.util.Calendar.HOUR_OF_DAY)
    private fun modelCount(alarm: Boolean): Int? = UiThread.call {
        val root = service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg } ?: return@call null
        val anchor = "$pkg:id/${if (alarm) "alarm_recycler_view" else "timer_view"}"
        val recycler = fixtureNodes(root).singleOrNull { it.viewIdResourceName == anchor } ?: return@call null
        recycler.collectionInfo?.let { it.rowCount * it.columnCount }
    }
    private fun create(op: String, value: JSONObject) {
        val alarm = op == "clock.alarms.create"
        val expected = (if (alarm) alarmModels else timerModels) + 1
        val dispatched = clocks.execute(op, Arguments(value.put("package", pkg)))
        assertFalse(dispatched.getBoolean("verified"))
        assertEquals("dispatched", dispatched.getString("outcome"))
        show(alarm)
        waitUntil("Native dispatch did not create the expected genuine vendor model before the next intent") { modelCount(alarm) == expected }
        assertEquals(expected, modelCount(alarm))
        if (alarm) alarmModels = expected else timerModels = expected
        record("native-model-created-$expected")
    }
    private fun fixtureNodes(root: AccessibilityNodeInfo): List<AccessibilityNodeInfo> {
        val nodes = ArrayList<AccessibilityNodeInfo>()
        fun visit(node: AccessibilityNodeInfo, depth: Int) {
            check(depth <= 30 && nodes.size < 400 && node.childCount <= 100) { "Vendor fixture exceeds complete traversal bounds" }
            if (!node.refresh()) throw VendorFixtureUnavailable("Vendor fixture node was detached during native model loading")
            check(node.childCount <= 100 && !node.isPassword && node.packageName?.toString() == pkg) { "Vendor fixture refreshed schema or ownership is excluded" }
            nodes.add(node)
            for (index in 0 until node.childCount) visit(node.getChild(index) ?: throw VendorFixtureUnavailable("Vendor fixture child was detached during native model loading"), depth + 1)
        }
        visit(root, 0)
        return nodes
    }
    private fun dismissVendorPromotion(alarm: Boolean): Boolean {
        GoogleClockProfile.requireInstalled(context)
        val candidate = UiThread.call {
            val root = service.safeRoot(pkg)
            val applications = service.windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            if (applications.size != 2 || applications.count { it.id == root.windowId && it.isActive && it.isFocused } != 1) return@call null
            if (applications.any { it.root?.packageName?.toString() != pkg }) return@call null
            val host = applications.single { it.id == root.windowId }
            val hostBounds = android.graphics.Rect().also { host.getBoundsInScreen(it) }
            applications.filter { !it.isActive && !it.isFocused && it.layer > host.layer }.mapNotNull { window ->
                val popupBounds = android.graphics.Rect().also { window.getBoundsInScreen(it) }
                if (popupBounds.isEmpty || !hostBounds.contains(popupBounds)) return@mapNotNull null
                val popup = window.root ?: return@mapNotNull null
                val nodes = fixtureNodes(popup)
                val body = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/body" && it.className?.toString() == "android.widget.TextView" && it.isVisibleToUser && it.text?.toString() in setOf("Set a consistent bedtime for better sleep", "You can find the privacy policy here") } ?: return@mapNotNull null
                val dismissal = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/dismiss_button" && it.className?.toString() == "android.widget.TextView" && it.isVisibleToUser && it.isEnabled && it.isClickable && it.text?.toString() == "Dismiss" } ?: return@mapNotNull null
                if (nodes.any { it.isPassword || it.packageName?.toString() != pkg } || nodes.any { it.viewIdResourceName in setOf("$pkg:id/onoff", "$pkg:id/play_pause_button", "$pkg:id/play_pause", "$pkg:id/delete_button", "$pkg:id/tertiary_button", "$pkg:id/save_button", "$pkg:id/alarm_recycler_view", "$pkg:id/timer_view") && it.isVisibleToUser }) return@mapNotNull null
                body.windowId.takeIf { it == dismissal.windowId }
            }.singleOrNull()
        } ?: return false
        record("vendor-promotion-before-dismissal")
        val count = modelCount(alarm) ?: error("Vendor collection disappeared under its promotion")
        rejected(setOf("UNSUPPORTED")) { ui(if (alarm) "clock.alarms.list" else "clock.timers.list") }
        rejected(setOf("UNSUPPORTED")) { ui(if (alarm) "clock.alarms.delete" else "clock.timers.cancel", args().put("label", "missing-under-promotion").apply { if (alarm) put("time", "00:00") }) }
        assertEquals(count, modelCount(alarm))
        GoogleClockProfile.requireInstalled(context)
        val dismissedBody = UiThread.call {
            Access.unlocked(service)
            val active = service.safeRoot(pkg)
            val windows = service.windows
            val applications = windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            assertEquals(2, applications.size)
            assertTrue(applications.all { it.root?.packageName?.toString() == pkg })
            assertEquals(1, applications.count { it.id == active.windowId && it.isActive && it.isFocused })
            assertEquals(1, windows.count { it.isActive })
            assertEquals(1, windows.count { it.isFocused })
            val host = applications.single { it.id == active.windowId }
            val hostNodes = fixtureNodes(host.root ?: error("Observed collection host disappeared"))
            val navigationBackgrounds = hostNodes.filter { it.viewIdResourceName == "android:id/navigationBarBackground" }.map { node ->
                android.graphics.Rect().also { node.getBoundsInScreen(it) }.let { GoogleClockBounds(it.left, it.top, it.right, it.bottom) }
            }.distinct()
            assertTrue(windows.filter { it.type != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }.all { window ->
                val bounds = android.graphics.Rect().also { window.getBoundsInScreen(it) }
                GoogleClockWindows.system(Build.VERSION.SDK_INT, window.type, window.isActive, window.isFocused, window.root?.packageName?.toString(), GoogleClockBounds(bounds.left, bounds.top, bounds.right, bounds.bottom), navigationBackgrounds)
            })
            val popupWindow = applications.single { it.id == candidate }
            assertFalse(popupWindow.isActive)
            assertFalse(popupWindow.isFocused)
            assertTrue(popupWindow.layer > host.layer)
            val hostBounds = android.graphics.Rect().also { host.getBoundsInScreen(it) }
            val popupBounds = android.graphics.Rect().also { popupWindow.getBoundsInScreen(it) }
            assertTrue(!popupBounds.isEmpty && hostBounds.contains(popupBounds))
            val popup = popupWindow.root ?: error("Observed vendor promotion disappeared before dismissal")
            val nodes = fixtureNodes(popup)
            assertEquals(pkg, popup.packageName?.toString())
            val body = nodes.single { it.viewIdResourceName == "$pkg:id/body" && it.isVisibleToUser }
            assertEquals("android.widget.TextView", body.className?.toString())
            assertTrue(body.text?.toString() in setOf("Set a consistent bedtime for better sleep", "You can find the privacy policy here"))
            assertFalse(nodes.any { it.viewIdResourceName in setOf("$pkg:id/onoff", "$pkg:id/play_pause_button", "$pkg:id/play_pause", "$pkg:id/delete_button", "$pkg:id/tertiary_button", "$pkg:id/save_button", "$pkg:id/alarm_recycler_view", "$pkg:id/timer_view") && it.isVisibleToUser })
            val dismissal = nodes.single { it.viewIdResourceName == "$pkg:id/dismiss_button" && it.isVisibleToUser }
            assertEquals("android.widget.TextView", dismissal.className?.toString())
            assertEquals("Dismiss", dismissal.text?.toString())
            assertEquals(candidate, body.windowId)
            assertEquals(candidate, dismissal.windowId)
            assertTrue(dismissal.isEnabled && dismissal.isClickable && !dismissal.isPassword)
            assertTrue(dismissal.actionList.any { it.id == AccessibilityNodeInfo.ACTION_CLICK })
            val dismissalBounds = android.graphics.Rect().also { dismissal.getBoundsInScreen(it) }
            assertTrue(!dismissalBounds.isEmpty && popupBounds.contains(dismissalBounds) && hostBounds.contains(dismissalBounds))
            service.click(dismissal)
            body.text.toString()
        }
        waitUntil("Genuine vendor promotion remained after its single explicit dismissal") { UiThread.call { service.windows.none { it.id == candidate } } }
        assertEquals("Promotion dismissal must not mutate native models", count, modelCount(alarm))
        dismissedCollectionPromotions.add(dismissedBody)
        record("vendor-promotion-dismissed")
        return true
    }
    private fun prepareAlarmDeleteAfterMusicPromotionRefusal(target: JSONObject, before: List<JSONObject>) {
        val originalWindow = UiThread.call { service.safeRoot(pkg).windowId }
        assertEquals(values(before), values(list(true)))
        UiThread.call {
            val root = service.safeRoot(pkg)
            assertEquals(originalWindow, root.windowId)
            val card = fixtureNodes(root).single { node ->
                node.viewIdResourceName == "$pkg:id/alarm_card" && node.isVisibleToUser &&
                    fixtureNodes(node).any { it.viewIdResourceName == "$pkg:id/label" && it.text?.toString() == target.getString("label") } &&
                    fixtureNodes(node).any { it.viewIdResourceName == "$pkg:id/digital_clock" && it.text?.toString() == target.getString("time") }
            }
            assertTrue(card.isEnabled && card.isClickable && !card.isPassword)
            assertTrue("Fixture must open the exact alarm detail without requesting deletion", card.performAction(AccessibilityNodeInfo.ACTION_CLICK))
        }
        record("alarm-detail-opening")
        fun musicPromotion(nodes: List<AccessibilityNodeInfo>): Boolean {
            val header = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/header" && it.isVisibleToUser && it.className?.toString() == "android.widget.TextView" } ?: return false
            val body = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/body" && it.isVisibleToUser && it.className?.toString() == "android.widget.TextView" } ?: return false
            val music = Regex("You can now choose music from (Calm|Spotify|Pandora|YouTube Music), (Calm|Spotify|Pandora|YouTube Music), (Calm|Spotify|Pandora|YouTube Music), & (Calm|Spotify|Pandora|YouTube Music)").matchEntire(body.text?.toString().orEmpty()) ?: return false
            return header.text?.toString() == "Wake up to music" && music.groupValues.drop(1).toSet() == setOf("Calm", "Spotify", "Pandora", "YouTube Music")
        }
        var promotionGraph: List<List<Any?>>? = null
        var promotionSince = 0L
        waitUntil("Genuine alarm detail music promotion was not exposed before deletion refusal") {
            val graph = try { UiThread.call {
                val windows = service.windows
                val applications = windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
                if (windows.size > 8 || applications.size != 2 || windows.count { it.isActive } != 1 || windows.count { it.isFocused } != 1) return@call null
                val roots = applications.associate { it.id to (it.root ?: return@call null) }
                if (roots.any { (window, root) -> root.windowId != window || root.packageName?.toString() != pkg }) return@call null
                val host = applications.singleOrNull { it.isActive && it.isFocused && it.id != originalWindow } ?: return@call null
                if (service.rootInActiveWindow?.windowId != host.id) return@call null
                val hostNodes = fixtureNodes(roots.getValue(host.id))
                if (hostNodes.singleOrNull { it.viewIdResourceName == "$pkg:id/clock" && it.isVisibleToUser }?.text?.toString() != target.getString("time") || hostNodes.singleOrNull { it.viewIdResourceName == "$pkg:id/alarm_label" && it.isVisibleToUser }?.text?.toString() != target.getString("label")) return@call null
                val popup = applications.single { it.id != host.id }
                if (popup.isActive || popup.isFocused || popup.layer <= host.layer || !musicPromotion(fixtureNodes(roots.getValue(popup.id)))) return@call null
                windows.sortedBy { it.id }.map { window ->
                    val bounds = android.graphics.Rect().also { window.getBoundsInScreen(it) }
                    listOf(window.id, window.type, window.isActive, window.isFocused, window.layer, bounds.left, bounds.top, bounds.right, bounds.bottom)
                }
            } } catch (_: VendorFixtureUnavailable) { null }
            val now = SystemClock.elapsedRealtime()
            if (graph == null || graph != promotionGraph) {
                promotionGraph = graph
                promotionSince = now
                false
            } else now - promotionSince >= 200
        }
        record("alarm-detail-music-promotion-exposed")
        try {
            ui("clock.alarms.delete", selection(target, true))
            fail("Stable genuine music promotion must refuse deletion before an alarm effect")
        } catch (e: BridgeFailure) {
            assertEquals("UNSUPPORTED", e.code)
            assertEquals("Clock promotion popup is not a collection host", e.message)
        }
        record("alarm-detail-music-promotion-refused")
        rejected(setOf("UNSUPPORTED")) { ui("clock.alarms.list") }
        GoogleClockProfile.requireInstalled(context)
        val candidate = UiThread.call {
            val root = service.safeRoot(pkg)
            val windows = service.windows
            val applications = windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            assertEquals(2, applications.size)
            assertTrue(applications.all { it.root?.packageName?.toString() == pkg })
            assertEquals(1, windows.count { it.isActive })
            assertEquals(1, windows.count { it.isFocused })
            val host = applications.single { it.id == root.windowId && it.isActive && it.isFocused }
            val hostBounds = android.graphics.Rect().also { host.getBoundsInScreen(it) }
            val hostNodes = fixtureNodes(root)
            GoogleClockAlarmDetail.requireSurface(hostNodes.filter { it.isVisibleToUser }.mapNotNull { it.viewIdResourceName?.takeIf { resource -> resource.startsWith("$pkg:id/") }?.substringAfter(":id/") })
            assertEquals(target.getString("time"), hostNodes.single { it.viewIdResourceName == "$pkg:id/clock" && it.isVisibleToUser }.text.toString())
            assertEquals(target.getString("label"), hostNodes.single { it.viewIdResourceName == "$pkg:id/alarm_label" && it.isVisibleToUser }.text.toString())
            val navigationBackgrounds = hostNodes.filter { it.viewIdResourceName == "android:id/navigationBarBackground" }.map { node ->
                android.graphics.Rect().also { node.getBoundsInScreen(it) }.let { GoogleClockBounds(it.left, it.top, it.right, it.bottom) }
            }.distinct()
            assertTrue(windows.filter { it.type != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }.all { window ->
                val bounds = android.graphics.Rect().also { window.getBoundsInScreen(it) }
                GoogleClockWindows.system(Build.VERSION.SDK_INT, window.type, window.isActive, window.isFocused, window.root?.packageName?.toString(), GoogleClockBounds(bounds.left, bounds.top, bounds.right, bounds.bottom), navigationBackgrounds)
            })
            val popup = applications.single { it.id != root.windowId }
            assertFalse(popup.isActive)
            assertFalse(popup.isFocused)
            assertTrue(popup.layer > host.layer)
            val popupBounds = android.graphics.Rect().also { popup.getBoundsInScreen(it) }
            assertTrue(!popupBounds.isEmpty && hostBounds.contains(popupBounds))
            val nodes = fixtureNodes(popup.root ?: error("Genuine music promotion disappeared before explicit fixture dismissal"))
            assertFalse(nodes.any { it.viewIdResourceName in setOf("$pkg:id/onoff", "$pkg:id/play_pause_button", "$pkg:id/play_pause", "$pkg:id/delete_button", "$pkg:id/tertiary_button", "$pkg:id/save_button", "$pkg:id/alarm_recycler_view", "$pkg:id/timer_view", "$pkg:id/clock", "$pkg:id/alarm_label", "$pkg:id/repeat_days") && it.isVisibleToUser })
            val body = nodes.single { it.viewIdResourceName == "$pkg:id/body" && it.isVisibleToUser }
            assertEquals("android.widget.TextView", body.className?.toString())
            assertTrue("Only the complete genuine music promotion may be dismissed", musicPromotion(nodes))
            val dismissal = nodes.single { it.viewIdResourceName == "$pkg:id/dismiss_button" && it.isVisibleToUser }
            assertEquals("android.widget.TextView", dismissal.className?.toString())
            assertEquals("Dismiss", dismissal.text?.toString())
            assertEquals(popup.id, body.windowId)
            assertEquals(popup.id, dismissal.windowId)
            assertTrue(dismissal.isEnabled && dismissal.isClickable && !dismissal.isPassword)
            assertTrue(dismissal.actionList.any { it.id == AccessibilityNodeInfo.ACTION_CLICK })
            val dismissalBounds = android.graphics.Rect().also { dismissal.getBoundsInScreen(it) }
            assertTrue(!dismissalBounds.isEmpty && popupBounds.contains(dismissalBounds) && hostBounds.contains(dismissalBounds))
            Access.unlocked(service)
            service.click(dismissal)
            popup.id
        }
        waitUntil("Genuine music promotion remained after one explicit fixture dismissal") { UiThread.call { service.windows.none { it.id == candidate } } }
        record("alarm-detail-music-promotion-dismissed")
        UiThread.call {
            val root = service.safeRoot(pkg)
            val nodes = fixtureNodes(root)
            GoogleClockAlarmDetail.requireSurface(nodes.filter { it.isVisibleToUser }.mapNotNull { it.viewIdResourceName?.takeIf { resource -> resource.startsWith("$pkg:id/") }?.substringAfter(":id/") })
            assertEquals(target.getString("time"), nodes.single { it.viewIdResourceName == "$pkg:id/clock" && it.isVisibleToUser }.text.toString())
            assertEquals(target.getString("label"), nodes.single { it.viewIdResourceName == "$pkg:id/alarm_label" && it.isVisibleToUser }.text.toString())
            assertNotEquals(originalWindow, root.windowId)
            assertEquals(1, service.windows.count { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION })
            Access.unlocked(service)
            assertTrue(service.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK))
        }
        waitUntil("Unmutated genuine collection did not return after closing alarm detail") {
            UiThread.call { service.rootInActiveWindow?.let { it.windowId == originalWindow && it.packageName?.toString() == pkg && fixtureNodes(it).count { node -> node.viewIdResourceName == "$pkg:id/alarm_recycler_view" && node.isVisibleToUser } == 1 } == true }
        }
        assertEquals("Popup refusal and explicit fixture dismissal must preserve every native alarm", values(before), values(list(true)))
        Thread.sleep(200)
        assertEquals("Fresh complete native readback must remain unchanged after fixture dismissal and Back", values(before), values(list(true)))
        UiThread.call {
            assertEquals(originalWindow, service.safeRoot(pkg).windowId)
            assertEquals(1, service.windows.count { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION })
        }
        record("alarm-detail-promotion-refused-without-alarm-mutation")
    }
    private fun show(alarm: Boolean) {
        val dispatched = clocks.execute(if (alarm) "clock.alarms.show" else "clock.timers.show", Arguments(args()))
        assertFalse(dispatched.getBoolean("verified"))
        assertEquals("dispatched", dispatched.getString("outcome"))
        val anchor = "$pkg:id/${if (alarm) "alarm_recycler_view" else "timer_view"}"
        waitUntil("Genuine Google Clock collection is missing") {
            UiThread.call {
                service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg }?.let { root -> fixtureNodes(root).any { it.viewIdResourceName == anchor && it.isVisibleToUser } } == true
            }
        }
        Thread.sleep(300)
        repeat(2) { if (dismissVendorPromotion(alarm)) Thread.sleep(300) }
        record(if (alarm) "alarms-shown" else "timers-shown")
    }
    private fun list(alarm: Boolean): List<JSONObject> {
        val result = ui(if (alarm) "clock.alarms.list" else "clock.timers.list")
        assertEquals("ui", result.getString("backend"))
        assertTrue(result.getBoolean("complete"))
        assertFalse(result.getBoolean("vendor_ids_available"))
        val items = result.getJSONArray(if (alarm) "alarms" else "timers")
        return (0 until items.length()).map { items.getJSONObject(it) }.onEach { assertTrue(it.isNull("id")) }
    }
    private fun countdown(label: String): String = UiThread.call {
        val root = service.safeRoot(pkg)
        var node = service.nodes(root).single { it.viewIdResourceName == "$pkg:id/timer_label" && it.isVisibleToUser && it.text?.toString() == label }
        repeat(30) {
            val children = service.nodes(node)
            val clocks = children.filter { it.viewIdResourceName == "$pkg:id/timer_text" && it.isVisibleToUser }
            if (clocks.size == 1 && children.count { it.viewIdResourceName == "$pkg:id/timer_label" && it.isVisibleToUser } == 1) return@call clocks.single().text?.toString() ?: error("Real timer countdown is unreadable")
            node = node.parent ?: error("Real timer row is unavailable")
        }
        error("Real timer row traversal exceeded bound")
    }
    private fun selection(value: JSONObject, alarm: Boolean) = args().put("label", value.getString("label")).apply { if (alarm) put("time", value.getString("time")) }
    private fun alarmRow(node: AccessibilityNodeInfo): AccessibilityNodeInfo {
        var current = node
        repeat(30) {
            if (current.viewIdResourceName == "$pkg:id/alarm_card") return current
            current = current.parent ?: error("Real alarm row is unavailable")
        }
        error("Real alarm row ancestry exceeded bound")
    }
    private fun timerAction(label: String, description: String): Boolean = UiThread.call {
        val root = service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg } ?: return@call false
        val nodes = fixtureNodes(root)
        val recycler = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/timer_view" } ?: return@call false
        val surface = GoogleClockTimerSurface.fromClass(recycler.className?.toString() ?: "")
        var row = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/timer_label" && it.isVisibleToUser && it.text?.toString() == label } ?: return@call false
        repeat(30) {
            if (row.collectionItemInfo != null) {
                val suffix = if (surface == GoogleClockTimerSurface.GASTOWN) " $label" else ""
                return@call fixtureNodes(row).count { it.viewIdResourceName == "$pkg:id/${surface.action}" && it.isVisibleToUser && it.contentDescription?.toString() == "$description$suffix" } == 1
            }
            row = row.parent ?: return@call false
        }
        false
    }
    private fun values(items: List<JSONObject>) = items.map { listOf(it.getString("label"), it.optString("time"), it.getString("state"), it.getString("schedule")) }.toSet()
    private fun rejected(expected: Set<String>, action: () -> Unit) {
        try { action(); fail("Unsafe Clock operation must be rejected before mutation") } catch (e: BridgeFailure) { assertTrue("Unexpected refusal ${e.code}: ${e.message}", e.code in expected) }
    }
    private fun verified(op: String, selection: JSONObject, outcome: String) {
        val previousWindow = UiThread.call { service.safeRoot(pkg).windowId }
        record("before-$op")
        val result = ui(op, selection)
        assertTrue(result.toString(), result.getBoolean("verified"))
        assertEquals(outcome, result.getString("outcome"))
        assertEquals("complete_collection_model_rebind", result.getString("scope"))
        UiThread.call {
            val root = service.safeRoot(pkg)
            assertNotEquals("Verified mutations must rebuild the genuine native collection", previousWindow, root.windowId)
            assertTrue(service.windows.none { it.id == previousWindow })
        }
        record("effect-$outcome")
    }
    private fun record(phase: String, refusal: BridgeFailure? = null) {
        val frame = JSONObject().put("phase", phase).put("sdk", Build.VERSION.SDK_INT).put("monotonic_ms", SystemClock.elapsedRealtime()).put("model", Build.MODEL).put("fingerprint", Build.FINGERPRINT)
        if (refusal != null) frame.put("refusal_code", refusal.code).put("refusal_message", refusal.message)
        UiThread.call {
            val access = BridgeAccessibility.current ?: return@call
            val active = access.rootInActiveWindow
            frame.put("package", active?.packageName?.toString()).put("window", active?.windowId)
            val windows = JSONArray()
            var remainingNodes = 400
            var remainingBytes = 200000
            access.windows.take(8).forEach { window ->
                val bounds = android.graphics.Rect().also { window.getBoundsInScreen(it) }
                val root = window.root
                val item = JSONObject().put("window", window.id).put("type", window.type).put("active", window.isActive).put("focused", window.isFocused).put("layer", window.layer).put("package", root?.packageName?.toString()).put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom)))
                val nodes = JSONArray()
                if (root != null && root.packageName?.toString() == pkg) access.nodes(root).forEach { node ->
                    if (!node.isPassword && node.packageName?.toString() == pkg) {
                        val nodeBounds = android.graphics.Rect().also { node.getBoundsInScreen(it) }
                        val entry = JSONObject().put("id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString()).put("text", node.text?.toString()?.take(256)).put("description", node.contentDescription?.toString()?.take(256)).put("visible", node.isVisibleToUser).put("enabled", node.isEnabled).put("clickable", node.isClickable).put("checkable", node.isCheckable).put("checked", node.isChecked).put("parent_id", node.parent?.viewIdResourceName ?: JSONObject.NULL).put("bounds", JSONArray(listOf(nodeBounds.left, nodeBounds.top, nodeBounds.right, nodeBounds.bottom))).put("actions", JSONArray(node.actionList.map { it.id }))
                        node.collectionInfo?.let { entry.put("collection", JSONObject().put("rows", it.rowCount).put("columns", it.columnCount).put("hierarchical", it.isHierarchical).put("selection", it.selectionMode)) }
                        node.collectionItemInfo?.let { entry.put("item", JSONObject().put("row", it.rowIndex).put("column", it.columnIndex).put("row_span", it.rowSpan).put("column_span", it.columnSpan).put("heading", it.isHeading).put("selected", it.isSelected)) }
                        val size = entry.toString().toByteArray().size
                        if (remainingNodes > 0 && size <= remainingBytes) {
                            nodes.put(entry)
                            remainingNodes--
                            remainingBytes -= size
                        } else frame.put("truncated", true)
                    }
                }
                item.put("nodes", nodes)
                windows.put(item)
                if (window.id == active?.windowId) frame.put("nodes", nodes)
            }
            frame.put("windows", windows).put("password_nodes_excluded", true).put("bounded", true)
        }
        if (evidence.length() < 24) evidence.put(frame) else evidence.put(23, frame)
        val chunks = frame.toString().chunked(1500)
        chunks.forEachIndexed { index, text -> android.util.Log.i("GoogleClockEvidence", JSONObject().put("test", testName.methodName).put("phase", phase).put("part", index).put("parts", chunks.size).put("json", text).toString()) }
    }

    @Before fun setup() {
        val arguments = InstrumentationRegistry.getArguments()
        assumeTrue("Genuine vendor runtime suite requires explicit googleClockFixture argument", arguments.containsKey("googleClockFixture"))
        assertEquals("googleClockFixture must be true when supplied", "true", arguments.getString("googleClockFixture"))
        val qemu = shell("getprop ro.kernel.qemu").trim()
        val bootQemu = shell("getprop ro.boot.qemu").trim()
        val model = shell("getprop ro.product.model").trim()
        assertTrue("This suite is for disposable API31/35 x86_64 QEMU CI emulators: sdk=${Build.VERSION.SDK_INT}, model=$model, qemu=$qemu, bootQemu=$bootQemu", Build.VERSION.SDK_INT in setOf(31, 35) && (qemu == "1" || bootQemu == "1") && Build.SUPPORTED_ABIS.contains("x86_64") && model == Build.MODEL && model == "Android SDK built for x86_64")
        try { context.packageManager.getPackageInfo(pkg, 0) } catch (_: android.content.pm.PackageManager.NameNotFoundException) { fail("Required genuine signed google-8.2.apk is missing; parent CI must install the exact fixture before instrumentation") }
        assertEquals("CI requires English locale", "en", context.resources.configuration.locales[0].language)
        originalTimeFormat = Settings.System.getString(context.contentResolver, Settings.System.TIME_12_24)
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        originalAccessibility = Settings.Secure.getInt(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0)
        optedIn = true
        shell("settings put system time_12_24 24")
        assertTrue("Installed Google Clock version, bytes or current signer differ from the exact fixture", GoogleClockProfile.installed(context))
        BridgeService.stop(context, true)
        assertTrue("Disposable vendor fixture reset failed", shell("pm clear $pkg").contains("Success"))
        shell("cmd appops set $pkg SCHEDULE_EXACT_ALARM allow")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant $pkg android.permission.POST_NOTIFICATIONS")
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        shell("settings put secure enabled_accessibility_services $component")
        shell("settings put secure accessibility_enabled 1")
        waitUntil("Real PhoneBridge Accessibility did not connect") { BridgeAccessibility.current != null }
        assertTrue(GoogleClockProfile.operations.all { it in ClockAdapters(context).capabilities() })
        show(true)
        defaultAlarms = list(true)
        assertEquals("The exact vendor database seeds two disabled alarms", 2, defaultAlarms.size)
        assertEquals(setOf("08:30", "09:00"), defaultAlarms.map { it.getString("time") }.toSet())
        assertTrue(defaultAlarms.all { it.getString("label").isEmpty() && !it.getBoolean("enabled") })
        alarmModels = defaultAlarms.size
    }

    @After fun cleanup() {
        if (!optedIn) return
        try {
            record("final-real-root")
            val directory = File(context.filesDir, "google-clock-ci").apply { mkdirs() }
            val report = File(directory, "api${Build.VERSION.SDK_INT}-${testName.methodName}.json")
            report.writeText(JSONObject().put("fixture", GoogleClockProfile.APK_SHA256).put("version", GoogleClockProfile.VERSION).put("signer", GoogleClockProfile.SIGNER_SHA256).put("frames", evidence).toString())
            instrumentation.sendStatus(0, Bundle().apply { putString("googleClockEvidence", report.absolutePath) })
        } finally {
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            shell("pm clear $pkg")
            shell("settings put system time_12_24 ${originalTimeFormat ?: "null"}")
            shell("settings put secure enabled_accessibility_services ${originalServices?.takeIf { it.isNotEmpty() } ?: "null"}")
            shell("settings put secure accessibility_enabled $originalAccessibility")
            BridgeService.stop(context, true)
        }
    }

    @Test fun genuineAlarmsExplicitDisableEnableDeleteAndColdRestartPreserveSurvivors() {
        val hour = alarmHour
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 31).put("label", "CI alarm, Start Alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 43).put("label", "CI unrelated alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        show(true)
        val initial = list(true)
        assertEquals(defaultAlarms.size + 2, initial.size)
        val target = initial.single { it.getString("label") == "CI alarm, Start Alarm" }
        assertEquals(String.format(java.util.Locale.US, "%02d:31", hour), target.getString("time"))
        assertEquals("every day", target.getString("schedule"))
        val survivors = initial.filter { it.getString("label") != target.getString("label") }
        assertEquals(values(defaultAlarms), values(survivors.filter { it.getString("label").isEmpty() }))
        assertEquals(1, survivors.count { it.getString("label") == "CI unrelated alarm" && it.getBoolean("enabled") })
        assertTrue(target.getBoolean("enabled"))
        val missingBefore = values(initial)
        rejected(setOf("NOT_FOUND")) { ui("clock.alarms.delete", args().put("label", "missing").put("time", target.getString("time"))) }
        assertEquals(missingBefore, values(list(true)))
        verified("clock.alarms.disable", selection(target, true), "changed")
        assertFalse(list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        assertEquals(values(survivors), values(list(true).filter { it.getString("label") != target.getString("label") }))
        shell("am force-stop $pkg")
        show(true)
        assertFalse("Disabled state was not persisted across genuine vendor process restart", list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        val unchanged = ui("clock.alarms.disable", selection(target, true))
        assertEquals("unchanged", unchanged.getString("outcome"))
        assertTrue(unchanged.getBoolean("verified"))
        assertEquals("complete_current_collection", unchanged.getString("scope"))
        verified("clock.alarms.enable", selection(target, true), "changed")
        shell("am force-stop $pkg")
        show(true)
        assertTrue(list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        prepareAlarmDeleteAfterMusicPromotionRefusal(target, list(true))
        verified("clock.alarms.delete", selection(target, true), "deleted")
        assertEquals(values(survivors), values(list(true)))
        shell("am force-stop $pkg")
        show(true)
        assertEquals(values(survivors), values(list(true)))
    }

    @Test fun genuineAlarmDetailMusicPromotionRejectsDeletionAndPreservesEveryAlarm() {
        val hour = alarmHour
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 31).put("label", "CI popup target alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 43).put("label", "CI popup survivor alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        show(true)
        val before = list(true)
        assertEquals(defaultAlarms.size + 2, before.size)
        shell("am force-stop $pkg")
        show(true)
        assertEquals("Cold fixture restart must preserve every alarm before opening the music promotion", values(before), values(list(true)))
        assertEquals("Privacy and bedtime each suppress music for their entire vendor process", listOf("You can find the privacy policy here", "Set a consistent bedtime for better sleep"), dismissedCollectionPromotions)
        shell("am force-stop $pkg")
        show(true)
        assertEquals("The music fixture must start a fresh process after both one-time collection promotions", 2, dismissedCollectionPromotions.size)
        assertEquals("Promotion-only process setup must preserve every alarm value", values(before), values(list(true)))
        prepareAlarmDeleteAfterMusicPromotionRefusal(before.single { it.getString("label") == "CI popup target alarm" }, before)
        shell("am force-stop $pkg")
        show(true)
        assertEquals(values(before), values(list(true)))
    }

    @Test fun genuineRunningPausedTimerDriftAndPreciseCancellationKeepUnrelatedTimer() {
        create("clock.timers.create", JSONObject().put("seconds", 7200).put("label", "CI timer, Start Alarm"))
        create("clock.timers.create", JSONObject().put("seconds", 5400).put("label", "CI survivor timer"))
        show(false)
        val initial = list(false)
        assertEquals(2, initial.size)
        val target = initial.single { it.getString("label") == "CI timer, Start Alarm" }
        val survivor = initial.single { it.getString("label") == "CI survivor timer" }
        assertEquals("running", target.getString("state"))
        val countdownBefore = countdown(target.getString("label"))
        Thread.sleep(1600)
        assertNotEquals("Genuine running countdown must drift while selection remains unchanged", countdownBefore, countdown(target.getString("label")))
        assertEquals(values(initial), values(list(false)))
        rejected(setOf("NOT_FOUND")) { ui("clock.timers.cancel", args().put("label", "missing")) }
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.cancel", args().put("id", "pretend-vendor-id")) }
        rejected(setOf("VALIDATION_ERROR")) { ui("clock.timers.cancel", args().put("label", target.getString("label")).put("time", "1:59:59")) }
        assertEquals(values(initial), values(list(false)))
        verified("clock.timers.pause", selection(target, false), "changed")
        assertEquals("paused", list(false).single { it.getString("label") == target.getString("label") }.getString("state"))
        assertEquals(values(listOf(survivor)), values(list(false).filter { it.getString("label") == survivor.getString("label") }))
        verified("clock.timers.resume", selection(target, false), "changed")
        Thread.sleep(1100)
        verified("clock.timers.cancel", selection(target, false), "deleted")
        assertEquals(values(listOf(survivor)), values(list(false)))
        shell("am force-stop $pkg")
        show(false)
        assertEquals(values(listOf(survivor)), values(list(false)))
        timerModels = 1
        create("clock.timers.create", JSONObject().put("seconds", 6300).put("label", "CI paused cancellation timer"))
        val pausedTarget = list(false).single { it.getString("label") == "CI paused cancellation timer" }
        verified("clock.timers.pause", selection(pausedTarget, false), "changed")
        val pausedBefore = countdown(pausedTarget.getString("label"))
        Thread.sleep(1100)
        assertEquals("Paused genuine countdown must not drift", pausedBefore, countdown(pausedTarget.getString("label")))
        assertEquals("paused", list(false).single { it.getString("label") == pausedTarget.getString("label") }.getString("state"))
        verified("clock.timers.cancel", selection(pausedTarget, false), "deleted")
        assertEquals(values(listOf(survivor)), values(list(false)))
        shell("am force-stop $pkg")
        show(false)
        assertEquals(values(listOf(survivor)), values(list(false)))
    }

    @Test fun genuineDuplicateTimerNamesAreNotSelectableAndRemainPresent() {
        create("clock.timers.create", JSONObject().put("seconds", 7200).put("label", "CI duplicate"))
        create("clock.timers.create", JSONObject().put("seconds", 5400).put("label", "CI duplicate"))
        show(false)
        val count = UiThread.call {
            val root = service.safeRoot(pkg)
            service.exact(root, "$pkg:id/timer_view").collectionInfo?.let { it.rowCount * it.columnCount }
        }
        assertEquals("Real API must have created two genuine models", 2, count)
        assertEquals(2, UiThread.call { service.nodes(service.safeRoot(pkg)).count { it.viewIdResourceName == "$pkg:id/timer_label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate" } })
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.cancel", args().put("label", "CI duplicate")) }
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.list") }
        assertEquals(count, UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/timer_view").collectionInfo?.let { it.rowCount * it.columnCount } })
        record("duplicates-remain-after-refusal")
    }

    @Test fun genuineDuplicateAlarmIdentityAndIncompleteCollectionRefuseBeforeClick() {
        val hour = alarmHour
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 31).put("label", "CI duplicate alarm").put("days", JSONArray(listOf(1, 2))))
        create("clock.alarms.create", JSONObject().put("hour", hour).put("minute", 31).put("label", "CI duplicate alarm").put("days", JSONArray(listOf(3, 4))))
        show(true)
        val duplicates = UiThread.call { service.nodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate alarm" }.size }
        assertEquals("Real vendor API must create separate models for different recurrence", 2, duplicates)
        val schedules = UiThread.call { fixtureNodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate alarm" }.map { label -> fixtureNodes(alarmRow(label)).single { it.viewIdResourceName == "$pkg:id/days_of_week" && it.isVisibleToUser }.contentDescription?.toString() }.toSet() }
        assertEquals(setOf("Monday, Tuesday", "Wednesday, Thursday"), schedules)
        val times = UiThread.call { fixtureNodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate alarm" }.map { label -> fixtureNodes(alarmRow(label)).single { it.viewIdResourceName == "$pkg:id/digital_clock" && it.isVisibleToUser }.text.toString() } }
        assertEquals(2, times.size)
        assertEquals(1, times.distinct().size)
        rejected(setOf("UNSUPPORTED")) { ui("clock.alarms.disable", args().put("label", "CI duplicate alarm").put("time", times.first())) }
        val duplicateStates = UiThread.call { fixtureNodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate alarm" }.map { label -> fixtureNodes(alarmRow(label)).single { it.viewIdResourceName == "$pkg:id/onoff" && it.isVisibleToUser }.isChecked } }
        assertEquals(listOf(true, true), duplicateStates)
        for (minute in 0..8) create("clock.alarms.create", JSONObject().put("hour", 21).put("minute", minute).put("label", "CI offscreen $minute").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        show(true)
        val count = UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/alarm_recycler_view").collectionInfo?.rowCount }
        assertTrue("Real collection must include offscreen alarms", count != null && count > 8)
        rejected(setOf("UNSUPPORTED")) { ui("clock.alarms.delete", args().put("label", "CI offscreen 0").put("time", "21:00")) }
        assertEquals(count, UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/alarm_recycler_view").collectionInfo?.rowCount })
        record("incomplete-collection-refused")
    }

    @Test fun genuinePauseThenLockReturnsAmbiguousWithoutRetryingTheEffect() {
        create("clock.timers.create", JSONObject().put("seconds", 7200).put("label", "CI interrupted timer"))
        show(false)
        val target = list(false).single()
        val executor = java.util.concurrent.Executors.newSingleThreadExecutor()
        val action = executor.submit<JSONObject> { ui("clock.timers.pause", selection(target, false)) }
        try {
            val deadline = SystemClock.elapsedRealtime() + 15000
            var paused = false
            while (!paused && SystemClock.elapsedRealtime() < deadline) {
                paused = timerAction("CI interrupted timer", "Start")
                if (!paused) Thread.sleep(5)
            }
            assertTrue("The genuine pause dispatch was not observed", paused)
            shell("input keyevent KEYCODE_SLEEP")
            try { action.get(15, java.util.concurrent.TimeUnit.SECONDS); fail("Interrupted post-dispatch readback must not claim success") } catch (e: java.util.concurrent.ExecutionException) {
                assertTrue(e.cause is BridgeFailure)
                assertEquals("AMBIGUOUS_STATE", (e.cause as BridgeFailure).code)
            }
        } finally {
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            executor.shutdownNow()
            assertTrue(executor.awaitTermination(15, java.util.concurrent.TimeUnit.SECONDS))
        }
        show(false)
        assertEquals("A retry must not resume or reset the actual paused timer", "paused", list(false).single().getString("state"))
        record("ambiguous-effect-not-retried")
    }

    @Test fun genuineMissingNamedIdentityUnknownWindowTimeFormatAndLockedPhoneRefuseEffects() {
        create("clock.timers.create", JSONObject().put("seconds", 7200))
        show(false)
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.cancel", args().put("label", "")) }
        UiThread.call { service.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        waitUntil("PhoneBridge did not become foreground") {
            MainActivity.resumed && UiThread.call { service.rootInActiveWindow?.packageName?.toString() == context.packageName }
        }
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.cancel", args().put("label", "unknown")) }
        shell("settings put system time_12_24 12")
        rejected(setOf("UNSUPPORTED")) { ui("clock.timers.cancel", args().put("label", "unknown")) }
        assertTrue(ClockAdapters(context).capabilities().isEmpty())
        shell("settings put system time_12_24 24")
        shell("input keyevent KEYCODE_SLEEP")
        waitUntil("CI emulator did not enter a locked/noninteractive state") { context.getSystemService(KeyguardManager::class.java).isKeyguardLocked || !context.getSystemService(android.os.PowerManager::class.java).isInteractive }
        rejected(setOf("PHONE_LOCKED")) { ui("clock.timers.cancel", args().put("label", "unknown")) }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        assertTrue(SamsungClockAdapter().supported("12.4.00.8", 1240008100L).isEmpty())
        assertTrue(GoogleClockAdapter(context).supported("9.2 (993392871)", 76120011L).isEmpty())
    }
}
