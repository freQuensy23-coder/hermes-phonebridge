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
    private val clocks get() = NativeClocks(context)
    private val service get() = BridgeAccessibility.current ?: error("Real PhoneBridge Accessibility service is not connected")

    private fun shell(command: String): String = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).executeShellCommand(command).use { parcel ->
        android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).use { it.readBytes().toString(Charsets.UTF_8) }
    }
    private fun waitUntil(message: String, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15000
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(100)
        }
        fail(message)
    }
    private fun args() = JSONObject().put("package", pkg)
    private fun ui(op: String, selection: JSONObject = args()) = ClockAdapters(context).execute(op, Arguments(selection))
    private var alarmModels = 0
    private var timerModels = 0
    private fun modelCount(alarm: Boolean): Int? = UiThread.call {
        val root = service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg } ?: return@call null
        val anchor = "$pkg:id/${if (alarm) "alarm_recycler_view" else "timer_view"}"
        val recycler = service.nodes(root).singleOrNull { it.viewIdResourceName == anchor && it.isVisibleToUser } ?: return@call null
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
            check(depth <= 30 && nodes.size < 400 && node.refresh() && node.childCount <= 100 && !node.isPassword && node.packageName?.toString() == pkg)
            nodes.add(node)
            for (index in 0 until node.childCount) visit(node.getChild(index) ?: error("Vendor fixture child unavailable"), depth + 1)
        }
        visit(root, 0)
        return nodes
    }
    private fun dismissBedtimePromotion(): Boolean {
        GoogleClockProfile.requireInstalled(context)
        val candidate = UiThread.call {
            val root = service.safeRoot(pkg)
            val applications = service.windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            if (applications.size != 2 || applications.count { it.id == root.windowId && it.isActive && it.isFocused } != 1) return@call null
            if (applications.any { it.root?.packageName?.toString() != pkg }) return@call null
            applications.mapNotNull { window ->
                val popup = window.root ?: return@mapNotNull null
                val nodes = fixtureNodes(popup)
                val body = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/body" && it.isVisibleToUser && it.text?.toString() == "Set a consistent bedtime for better sleep" } ?: return@mapNotNull null
                val dismissal = nodes.singleOrNull { it.viewIdResourceName == "$pkg:id/dismiss_button" && it.isVisibleToUser && it.isEnabled && it.isClickable && it.text?.toString() == "Dismiss" } ?: return@mapNotNull null
                if (nodes.any { it.isPassword || it.packageName?.toString() != pkg } || nodes.any { it.viewIdResourceName in setOf("$pkg:id/onoff", "$pkg:id/play_pause_button", "$pkg:id/delete_button", "$pkg:id/save_button") && it.isVisibleToUser }) return@mapNotNull null
                body.windowId.takeIf { it == dismissal.windowId }
            }.singleOrNull()
        } ?: return false
        record("bedtime-promotion-before-dismissal")
        rejected(setOf("UNSUPPORTED")) { ui("clock.alarms.list") }
        GoogleClockProfile.requireInstalled(context)
        UiThread.call {
            Access.unlocked(service)
            val active = service.safeRoot(pkg)
            val windows = service.windows
            val applications = windows.filter { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }
            assertEquals(2, applications.size)
            assertTrue(applications.all { it.root?.packageName?.toString() == pkg })
            assertEquals(1, applications.count { it.id == active.windowId && it.isActive && it.isFocused })
            assertTrue(windows.filter { it.type != android.view.accessibility.AccessibilityWindowInfo.TYPE_APPLICATION }.all { it.type == android.view.accessibility.AccessibilityWindowInfo.TYPE_SYSTEM && !it.isActive && !it.isFocused && it.root?.packageName?.toString() == "com.android.systemui" })
            val popup = applications.single { it.id == candidate }.root ?: error("Observed vendor promotion disappeared before dismissal")
            val nodes = fixtureNodes(popup)
            assertEquals(pkg, popup.packageName?.toString())
            assertEquals("Set a consistent bedtime for better sleep", nodes.single { it.viewIdResourceName == "$pkg:id/body" && it.isVisibleToUser }.text?.toString())
            val dismissal = nodes.single { it.viewIdResourceName == "$pkg:id/dismiss_button" && it.isVisibleToUser }
            assertEquals("Dismiss", dismissal.text?.toString())
            assertTrue(dismissal.isEnabled && dismissal.isClickable && !dismissal.isPassword)
            service.click(dismissal)
        }
        waitUntil("Genuine vendor promotion remained after its single explicit dismissal") { UiThread.call { service.windows.none { it.id == candidate } } }
        record("bedtime-promotion-dismissed")
        return true
    }
    private fun show(alarm: Boolean) {
        clocks.execute(if (alarm) "clock.alarms.show" else "clock.timers.show", Arguments(args()))
        val anchor = "$pkg:id/${if (alarm) "alarm_recycler_view" else "timer_view"}"
        waitUntil("Genuine Google Clock Gastown collection is missing") {
            UiThread.call {
                service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg }?.let { root -> service.nodes(root).any { it.viewIdResourceName == anchor && it.isVisibleToUser } } == true
            }
        }
        Thread.sleep(300)
        dismissBedtimePromotion()
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
    private fun values(items: List<JSONObject>) = items.map { listOf(it.getString("label"), it.optString("time"), it.getString("state"), it.getString("schedule")) }.toSet()
    private fun rejected(expected: Set<String>, action: () -> Unit) {
        try { action(); fail("Unsafe Clock operation must be rejected before mutation") } catch (e: BridgeFailure) { assertTrue("Unexpected refusal ${e.code}: ${e.message}", e.code in expected) }
    }
    private fun verified(result: JSONObject, outcome: String) {
        assertTrue(result.toString(), result.getBoolean("verified"))
        assertEquals(outcome, result.getString("outcome"))
        assertEquals("complete_collection_model_rebind", result.getString("scope"))
        record("effect-$outcome")
    }
    private fun record(phase: String) {
        val frame = JSONObject().put("phase", phase).put("sdk", Build.VERSION.SDK_INT).put("monotonic_ms", SystemClock.elapsedRealtime()).put("model", Build.MODEL).put("fingerprint", Build.FINGERPRINT)
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
        create("clock.alarms.create", JSONObject().put("hour", 17).put("minute", 31).put("label", "CI alarm, Start Alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        create("clock.alarms.create", JSONObject().put("hour", 19).put("minute", 43).put("label", "CI unrelated alarm").put("days", JSONArray(listOf(1, 2, 3, 4, 5, 6, 7))))
        show(true)
        val initial = list(true)
        assertEquals(2, initial.size)
        val target = initial.single { it.getString("label") == "CI alarm, Start Alarm" }
        val survivor = initial.single { it.getString("label") == "CI unrelated alarm" }
        assertTrue(target.getBoolean("enabled"))
        val missingBefore = values(initial)
        rejected(setOf("NOT_FOUND")) { ui("clock.alarms.delete", args().put("label", "missing").put("time", target.getString("time"))) }
        assertEquals(missingBefore, values(list(true)))
        verified(ui("clock.alarms.disable", selection(target, true)), "changed")
        assertFalse(list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        assertEquals(values(listOf(survivor)), values(list(true).filter { it.getString("label") == survivor.getString("label") }))
        shell("am force-stop $pkg")
        show(true)
        assertFalse("Disabled state was not persisted across genuine vendor process restart", list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        assertEquals("unchanged", ui("clock.alarms.disable", selection(target, true)).getString("outcome"))
        verified(ui("clock.alarms.enable", selection(target, true)), "changed")
        shell("am force-stop $pkg")
        show(true)
        assertTrue(list(true).single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        verified(ui("clock.alarms.delete", selection(target, true)), "deleted")
        assertEquals(values(listOf(survivor)), values(list(true)))
        shell("am force-stop $pkg")
        show(true)
        assertEquals(values(listOf(survivor)), values(list(true)))
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
        verified(ui("clock.timers.pause", selection(target, false)), "changed")
        assertEquals("paused", list(false).single { it.getString("label") == target.getString("label") }.getString("state"))
        assertEquals(values(listOf(survivor)), values(list(false).filter { it.getString("label") == survivor.getString("label") }))
        verified(ui("clock.timers.resume", selection(target, false)), "changed")
        Thread.sleep(1100)
        verified(ui("clock.timers.cancel", selection(target, false)), "deleted")
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
        create("clock.alarms.create", JSONObject().put("hour", 17).put("minute", 31).put("label", "CI duplicate alarm").put("days", JSONArray(listOf(1, 2))))
        create("clock.alarms.create", JSONObject().put("hour", 17).put("minute", 31).put("label", "CI duplicate alarm").put("days", JSONArray(listOf(3, 4))))
        show(true)
        val duplicates = UiThread.call { service.nodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/label" && it.isVisibleToUser && it.text?.toString() == "CI duplicate alarm" }.size }
        assertEquals("Real vendor API must create separate models for different recurrence", 2, duplicates)
        val schedules = UiThread.call { service.nodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/days_of_week" && it.isVisibleToUser }.map { it.contentDescription?.toString() }.toSet() }
        assertEquals(setOf("Monday, Tuesday", "Wednesday, Thursday"), schedules)
        val times = UiThread.call { service.nodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/digital_clock" && it.isVisibleToUser }.map { it.text.toString() } }
        assertEquals(2, times.size)
        assertEquals(1, times.distinct().size)
        rejected(setOf("UNSUPPORTED")) { ui("clock.alarms.disable", args().put("label", "CI duplicate alarm").put("time", times.first())) }
        assertTrue(UiThread.call { service.nodes(service.safeRoot(pkg)).filter { it.viewIdResourceName == "$pkg:id/onoff" && it.isVisibleToUser }.all { it.isChecked } })
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
                paused = UiThread.call { service.rootInActiveWindow?.takeIf { it.packageName?.toString() == pkg }?.let { root -> service.nodes(root).any { it.viewIdResourceName == "$pkg:id/play_pause_button" && it.isVisibleToUser && it.contentDescription?.toString() == "Start CI interrupted timer" } } == true }
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
        waitUntil("PhoneBridge did not become foreground") { MainActivity.resumed }
        rejected(setOf("FOREGROUND_REQUIRED")) { ui("clock.timers.cancel", args().put("label", "unknown")) }
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
