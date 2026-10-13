package org.nous.phonebridge

import android.app.KeyguardManager
import android.app.UiAutomation
import android.content.Intent
import android.os.Build
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Rule
import org.junit.Test
import org.junit.rules.TestName
import org.junit.runner.RunWith
import java.util.UUID
import java.util.concurrent.Executors
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicBoolean

@RunWith(AndroidJUnit4::class)
class SamsungClockInstrumentationTest {
    @get:Rule val testName = TestName()
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val pkg = SamsungClockProfile.PACKAGE
    private val prefix = "PB-${UUID.randomUUID().toString().take(8)}"
    private val fixtureAlarms = mutableSetOf<String>()
    private val fixtureTimers = mutableSetOf<String>()
    private lateinit var evidence: SamsungClockEvidence
    private var configured = false
    private var originalServices: String? = null
    private var originalAccessibility = 0
    private val service get() = BridgeAccessibility.current ?: error("Real Samsung PhoneBridge Accessibility is not connected")
    private fun args() = JSONObject().put("package", pkg)
    private fun shell(command: String): String = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).use { it.readBytes().toString(Charsets.UTF_8) } }
    private fun waitUntil(message: String, predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 15000
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(100)
        }
        fail(message)
    }
    private fun foreground() {
        instrumentation.runOnMainSync { context.startActivity(Intent(context, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)) }
        waitUntil("PhoneBridge fixture foreground is required for typed native intents") { MainActivity.resumed }
    }
    private fun show(alarm: Boolean) {
        foreground()
        val result = NativeClocks(context).execute(if (alarm) "clock.alarms.show" else "clock.timers.show", Arguments(args()))
        assertFalse(result.getBoolean("verified"))
        waitUntil("Samsung vendor screen did not become foreground") { UiThread.call { service.rootInActiveWindow?.packageName?.toString() == pkg } }
        Thread.sleep(500)
    }
    private fun ui(op: String, selection: JSONObject = args()) = ClockAdapters(context).execute(op, Arguments(selection))
    private fun list(alarm: Boolean): List<JSONObject> {
        val result = ui(if (alarm) "clock.alarms.list" else "clock.timers.list")
        assertEquals("ui", result.getString("backend"))
        assertTrue(result.getBoolean("complete"))
        assertFalse(result.getBoolean("vendor_ids_available"))
        val values = result.getJSONArray(if (alarm) "alarms" else "timers")
        return (0 until values.length()).map { values.getJSONObject(it) }.onEach { assertTrue(it.isNull("id")) }
    }
    private fun selection(value: JSONObject, alarm: Boolean) = args().put("label", value.getString("label")).put("state", value.getString("state")).apply { if (alarm) put("time", value.getString("time")) }
    private fun values(items: List<JSONObject>) = items.map { listOf(it.getString("label"), it.optString("time"), it.getString("state"), it.getString("schedule")) }.groupingBy { it }.eachCount()
    private fun rejected(code: String, action: () -> Unit) {
        try { action(); fail("Samsung unsafe operation must refuse without positive proof") } catch (e: BridgeFailure) { assertEquals(e.message, code, e.code) }
    }
    private fun changed(result: JSONObject, outcome: String) {
        assertTrue(result.toString(), result.getBoolean("verified"))
        assertEquals(outcome, result.getString("outcome"))
        assertEquals("complete_collection_stable_ui_readback", result.getString("scope"))
        assertFalse(result.getBoolean("durable_scheduling_verified"))
        evidence.capture("$outcome-${result.getString("label")}")
    }
    private fun create(alarm: Boolean, name: String, hour: Int = 17, minute: Int = 31, seconds: Int = 7200, days: List<Int> = listOf(1, 2, 3, 4, 5, 6, 7), requireComplete: Boolean = true) {
        val label = "$prefix-$name"
        if (alarm) fixtureAlarms.add(label) else fixtureTimers.add(label)
        foreground()
        val request = args().put("label", label).apply {
            if (alarm) put("hour", hour).put("minute", minute).put("days", JSONArray(days)) else put("seconds", seconds)
        }
        val result = NativeClocks(context).execute(if (alarm) "clock.alarms.create" else "clock.timers.create", Arguments(request))
        assertEquals("native_intent", result.getString("backend"))
        assertEquals("dispatched", result.getString("outcome"))
        assertFalse("Native creation dispatch must not pretend to be fixture readback", result.getBoolean("verified"))
        show(alarm)
        waitUntil("Native Samsung creation did not expose its exact unique synthetic label; dispatch is not success") {
            UiThread.call { service.nodes(service.safeRoot(pkg)).any { it.isVisibleToUser && it.viewIdResourceName == "$pkg:id/${if (alarm) "alarm_list_alarm_name" else "timer_list_timer_name"}" && it.text?.toString() == label } }
        }
        if (requireComplete) assertEquals(1, list(alarm).count { it.getString("label") == label })
        evidence.capture("native-fixture-$name")
    }
    private fun rowNodes(label: String, alarm: Boolean): List<AccessibilityNodeInfo> {
        val root = service.safeRoot(pkg)
        var node = service.nodes(root).single { it.isVisibleToUser && it.viewIdResourceName == "$pkg:id/${if (alarm) "alarm_list_alarm_name" else "timer_list_timer_name"}" && it.text?.toString() == label }
        repeat(30) {
            if (node.viewIdResourceName == "$pkg:id/${if (alarm) "alarm_list_cardView" else "timer_list_Cardview"}") return service.nodes(node)
            node = node.parent ?: error("Samsung source fixture card is missing")
        }
        error("Samsung fixture source card ancestry exceeds bound")
    }
    private fun countdown(label: String): String = UiThread.call { rowNodes(label, false).single { it.viewIdResourceName == "$pkg:id/timer_time" && it.isVisibleToUser }.text.toString() }
    private fun coldReadback(alarm: Boolean, expected: List<JSONObject>) {
        val beforePid = shell("pidof $pkg").trim()
        assertTrue("Genuine vendor foreground process must be observable before cold readback", beforePid.matches(Regex("[0-9]+(?: [0-9]+)*")))
        shell("am force-stop $pkg")
        waitUntil("Vendor process did not actually stop; cold persistence is unproven") { shell("pidof $pkg").trim().isEmpty() }
        show(alarm)
        val afterPid = shell("pidof $pkg").trim()
        assertTrue(afterPid.matches(Regex("[0-9]+(?: [0-9]+)*")))
        assertNotEquals("A cold vendor restart needs a distinct observed process", beforePid, afterPid)
        assertEquals("Samsung model state must persist across a real vendor process restart", values(expected), values(list(alarm)))
        evidence.capture("cold-model-readback")
    }

    @Before fun setup() {
        evidence = SamsungClockEvidence(context, testName.methodName)
        val inventory = evidence.inventory()
        android.util.Log.i("SamsungClockEvidence", inventory.toString())
        assertEquals("Positive Samsung suite requires exact opt-in on a disposable physical stock Samsung; unknown devices are UNSUPPORTED, never a passing positive proof", "source-12.5.07.13-disposable-physical", InstrumentationRegistry.getArguments().getString("samsungClockFixture"))
        assertEquals("samsung", Build.MANUFACTURER.lowercase(java.util.Locale.ROOT))
        assertEquals("Inspected Samsung APK requires API36; API31/35 emulator results are not OEM proof", 36, Build.VERSION.SDK_INT)
        assertNotEquals("Physical Samsung fixture cannot be QEMU", "1", shell("getprop ro.kernel.qemu").trim())
        assertNotEquals("Physical Samsung fixture cannot be QEMU", "1", shell("getprop ro.boot.qemu").trim())
        assertTrue("UNSUPPORTED installed Samsung package/version/signature/APK/locale: $inventory", SamsungClockProfile.installed(context))
        assertFalse("Disposable Samsung must already be unlocked; fixture will not bypass a secure keyguard", context.getSystemService(KeyguardManager::class.java).isKeyguardLocked)
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        originalAccessibility = Settings.Secure.getInt(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED, 0)
        val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
        val services = (originalServices?.split(':') ?: emptyList()).filter { it.isNotEmpty() }.plus(component).distinct().joinToString(":")
        assertTrue(services.matches(Regex("[A-Za-z0-9_./:$]+")))
        configured = true
        shell("settings put secure enabled_accessibility_services '$services'")
        shell("settings put secure accessibility_enabled 1")
        waitUntil("Real PhoneBridge Accessibility service did not connect") { BridgeAccessibility.current != null }
        assertEquals(SamsungClockProfile.operations, SamsungClockAdapter().supported(SamsungClockProfile.VERSION, SamsungClockProfile.CODE))
        evidence.capture("setup")
    }

    @After fun cleanup() {
        if (!::evidence.isInitialized) return
        try {
            if (configured) {
                for (alarm in listOf(true, false)) {
                    val labels = if (alarm) fixtureAlarms else fixtureTimers
                    if (labels.isEmpty()) continue
                    try {
                        show(alarm)
                        val items = list(alarm)
                        for (item in items.filter { it.getString("label") in labels }) ui(if (alarm) "clock.alarms.delete" else "clock.timers.cancel", selection(item, alarm))
                    } catch (e: Exception) {
                        android.util.Log.e("SamsungClockEvidence", "Synthetic fixtures retained after fail-closed cleanup; no pm clear or generic delete: ${e.javaClass.simpleName}")
                    }
                }
                evidence.capture("final")
            }
        } finally {
            evidence.flush()
            if (configured) {
                val restored = originalServices?.takeIf { it.isNotEmpty() }
                if (restored != null && restored.matches(Regex("[A-Za-z0-9_./:$]+"))) shell("settings put secure enabled_accessibility_services '$restored'") else shell("settings delete secure enabled_accessibility_services")
                shell("settings put secure accessibility_enabled $originalAccessibility")
            }
        }
    }

    @Test fun genuineAlarmDisableEnableDeleteAndPersistentDecoySafety() {
        create(true, "alarm, Delete", 17, 31)
        create(true, "alarm-decoy", 19, 43)
        val before = list(true)
        val target = before.single { it.getString("label") == "$prefix-alarm, Delete" }
        val survivors = before.filter { it.getString("label") != target.getString("label") }
        assertTrue(target.getBoolean("enabled"))
        evidence.capture("alarm-positive-baseline", true)
        rejected("NOT_FOUND") { ui("clock.alarms.delete", args().put("label", "$prefix-missing").put("time", target.getString("time"))) }
        rejected("NOT_FOUND") { ui("clock.alarms.delete", args().put("label", target.getString("label")).put("time", "23:59")) }
        rejected("UNSUPPORTED") { ui("clock.alarms.disable", selection(target, true).put("state", "disabled")) }
        assertEquals(values(before), values(list(true)))
        changed(ui("clock.alarms.disable", selection(target, true)), "changed")
        val disabled = list(true)
        assertEquals(values(survivors), values(disabled.filter { it.getString("label") != target.getString("label") }))
        assertFalse(disabled.single { it.getString("label") == target.getString("label") }.getBoolean("enabled"))
        coldReadback(true, disabled)
        val disabledTarget = list(true).single { it.getString("label") == target.getString("label") }
        assertEquals("unchanged", ui("clock.alarms.disable", selection(disabledTarget, true)).getString("outcome"))
        changed(ui("clock.alarms.enable", selection(disabledTarget, true)), "changed")
        coldReadback(true, list(true))
        val enabledTarget = list(true).single { it.getString("label") == target.getString("label") }
        UiThread.call {
            val card = rowNodes(target.getString("label"), true).single { it.viewIdResourceName == "$pkg:id/alarm_list_cardView" }
            assertTrue(card.isLongClickable && card.isEnabled)
            assertTrue(card.performAction(AccessibilityNodeInfo.ACTION_LONG_CLICK))
        }
        waitUntil("Genuine Samsung context menu full header is missing") {
            UiThread.call { service.nodes(service.safeRoot(pkg)).any { it.isVisibleToUser && it.text?.toString() == "${target.getString("time")} ${target.getString("label")}" } }
        }
        evidence.capture("genuine-alarm-context-menu", true)
        UiThread.call { assertTrue(service.performGlobalAction(android.accessibilityservice.AccessibilityService.GLOBAL_ACTION_BACK)) }
        Thread.sleep(400)
        assertEquals(values(survivors + enabledTarget), values(list(true)))
        changed(ui("clock.alarms.delete", selection(enabledTarget, true)), "deleted")
        assertEquals(values(survivors), values(list(true)))
        coldReadback(true, survivors)
    }

    @Test fun genuineTimerPauseResumePreciseCancelAndCountdownDriftPreserveDecoy() {
        create(false, "timer, Delete", seconds = 7200)
        create(false, "timer-decoy", seconds = 5400)
        val before = list(false)
        val target = before.single { it.getString("label") == "$prefix-timer, Delete" }
        val survivors = before.filter { it.getString("label") != target.getString("label") }
        assertEquals("running", target.getString("state"))
        val previousTime = countdown(target.getString("label"))
        Thread.sleep(1600)
        assertNotEquals(previousTime, countdown(target.getString("label")))
        assertEquals(values(before), values(list(false)))
        rejected("UNSUPPORTED") { ui("clock.timers.cancel", selection(target, false).put("state", "paused")) }
        rejected("NOT_FOUND") { ui("clock.timers.cancel", args().put("label", "$prefix-missing").put("state", "running")) }
        rejected("UNSUPPORTED") { ui("clock.timers.cancel", args().put("id", "fake-vendor-id")) }
        rejected("VALIDATION_ERROR") { ui("clock.timers.cancel", selection(target, false).put("time", previousTime)) }
        assertEquals(values(before), values(list(false)))
        changed(ui("clock.timers.pause", selection(target, false)), "changed")
        val paused = list(false).single { it.getString("label") == target.getString("label") }
        assertEquals("paused", paused.getString("state"))
        assertEquals(values(survivors), values(list(false).filter { it.getString("label") != target.getString("label") }))
        evidence.capture("paused-positive-readback", true)
        changed(ui("clock.timers.resume", selection(paused, false)), "changed")
        val resumed = list(false).single { it.getString("label") == target.getString("label") }
        assertEquals("running", resumed.getString("state"))
        changed(ui("clock.timers.cancel", selection(resumed, false)), "deleted")
        assertEquals(values(survivors), values(list(false)))
        coldReadback(false, survivors)
    }

    @Test fun genuineDuplicateTimersRefuseSelectionWithoutDeletingEitherModel() {
        create(false, "duplicate", seconds = 7200)
        create(false, "duplicate", seconds = 5400, requireComplete = false)
        val count = UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/multi_timer_list").collectionInfo?.rowCount }
        val exposed = UiThread.call { service.nodes(service.safeRoot(pkg)).count { it.isVisibleToUser && it.viewIdResourceName == "$pkg:id/timer_list_timer_name" && it.text?.toString() == "$prefix-duplicate" } }
        assertEquals("Native Samsung fixture must contain two actual duplicate display names", 2, exposed)
        rejected("UNSUPPORTED") { ui("clock.timers.cancel", args().put("label", "$prefix-duplicate").put("state", "running")) }
        rejected("UNSUPPORTED") { ui("clock.timers.list") }
        assertEquals(count, UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/multi_timer_list").collectionInfo?.rowCount })
        assertEquals(exposed, UiThread.call { service.nodes(service.safeRoot(pkg)).count { it.isVisibleToUser && it.viewIdResourceName == "$pkg:id/timer_list_timer_name" && it.text?.toString() == "$prefix-duplicate" } })
        evidence.capture("duplicate-negative-proof", true)
    }

    @Test fun genuineOffscreenCollectionRefusesDestructiveScope() {
        for (index in 0..8) create(true, "offscreen-$index", 21, index, requireComplete = false)
        val count = UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/alarm_list").collectionInfo?.rowCount }
        assertTrue("Actual vendor model count must prove the large fixture, not a partial visible list", count != null && count >= 9)
        rejected("UNSUPPORTED") { ui("clock.alarms.delete", args().put("label", "$prefix-offscreen-0").put("time", "21:00")) }
        assertEquals(count, UiThread.call { service.exact(service.safeRoot(pkg), "$pkg:id/alarm_list").collectionInfo?.rowCount })
        evidence.capture("offscreen-negative-proof", true)
    }

    @Test fun genuineExpiredGuardWrongForegroundAndLockedPhoneDoNotClick() {
        create(false, "guard", seconds = 7200)
        val initial = list(false)
        val target = initial.single { it.getString("label") == "$prefix-guard" }
        ExecutionGuard.install(System.currentTimeMillis() / 1000 - 1) { true }
        try { rejected("EXPIRED") { SamsungClockAdapter().execute(service, "clock.timers.pause", Arguments(selection(target, false))) } } finally { ExecutionGuard.clear() }
        assertEquals(values(initial), values(list(false)))
        foreground()
        rejected("FOREGROUND_REQUIRED") { ui("clock.timers.cancel", selection(target, false)) }
        shell("input keyevent KEYCODE_SLEEP")
        try {
            waitUntil("Disposable Samsung did not become noninteractive") { !context.getSystemService(android.os.PowerManager::class.java).isInteractive }
            rejected("PHONE_LOCKED") { ui("clock.timers.cancel", selection(target, false)) }
        } finally { shell("input keyevent KEYCODE_WAKEUP") }
        assertFalse("CI requires an already-unlocked disposable phone, not keyguard bypass", context.getSystemService(KeyguardManager::class.java).isKeyguardLocked)
        show(false)
        assertEquals(values(initial), values(list(false)))
        evidence.capture("execution-guards-negative-proof", true)
    }

    @Test fun genuineRevokedAfterPauseBecomesAmbiguousAndNeverRetriesEffect() {
        create(false, "interrupted", seconds = 7200)
        create(false, "decoy", seconds = 5400)
        val target = list(false).single { it.getString("label") == "$prefix-interrupted" }
        val permitted = AtomicBoolean(true)
        val executor = Executors.newSingleThreadExecutor()
        val action = executor.submit<JSONObject> {
            ExecutionGuard.install(System.currentTimeMillis() / 1000 + 30) { permitted.get() }
            try { ui("clock.timers.pause", selection(target, false)) } finally { ExecutionGuard.clear() }
        }
        try {
            waitUntil("Samsung actual selected pause was not observed") {
                UiThread.call { rowNodes(target.getString("label"), false).any { it.viewIdResourceName == "$pkg:id/list_action_btn" && it.isVisibleToUser && it.contentDescription?.toString() == "Resume" } }
            }
            permitted.set(false)
            try { action.get(15, TimeUnit.SECONDS); fail("Post-effect revocation must not claim verified success") } catch (e: java.util.concurrent.ExecutionException) {
                assertTrue(e.cause is BridgeFailure)
                assertEquals("AMBIGUOUS_STATE", (e.cause as BridgeFailure).code)
            }
        } finally { executor.shutdownNow(); assertTrue(executor.awaitTermination(15, TimeUnit.SECONDS)) }
        assertEquals("No blind retry may resume the paused native timer", "paused", list(false).single { it.getString("label") == target.getString("label") }.getString("state"))
        evidence.capture("post-effect-ambiguity-negative-proof", true)
    }
}
