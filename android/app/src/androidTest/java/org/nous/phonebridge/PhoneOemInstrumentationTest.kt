package org.nous.phonebridge

import android.app.ActivityManager
import android.app.Notification
import android.app.NotificationManager
import android.app.UiAutomation
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.ParcelFileDescriptor
import android.os.SystemClock
import android.provider.Settings
import android.view.accessibility.AccessibilityNodeInfo
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.io.File
import java.security.MessageDigest
import java.util.Calendar
import java.util.UUID
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

@RunWith(AndroidJUnit4::class)
class PhoneOemInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val arguments = InstrumentationRegistry.getArguments()
    private val client = BridgeHttp.client.newBuilder().callTimeout(10, TimeUnit.SECONDS).build()
    private val report = JSONObject()
    private val frames = JSONArray()
    private val component = "org.nous.phonebridge/org.nous.phonebridge.BridgeAccessibility"
    private val stockPackages = listOf("com.google.android.deskclock", "com.sec.android.app.clockpackage")
    private val requiredUi = setOf("clock.alarms.list", "clock.alarms.disable", "clock.alarms.enable", "clock.alarms.delete", "clock.timers.list", "clock.timers.pause", "clock.timers.resume", "clock.timers.cancel")
    private lateinit var server: String
    private lateinit var admin: String
    private lateinit var pkg: String
    private lateinit var binding: JSONObject
    private var credentials: DeviceCredentials? = null
    private var scenario: ActivityScenario<MainActivity>? = null
    private var originalServices: String? = null
    private var originalTimeFormat: String? = null
    private var originalAccessibility: String? = null
    private var permissionsChanged = false
    private var deadline = 0L
    private val directory get() = File(context.getExternalFilesDir(null) ?: error("Test Lab external files unavailable"), "phone-oem").apply { check(mkdirs() || isDirectory) }

    private fun shell(command: String): String = instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).executeShellCommand(command).use { descriptor ->
        ParcelFileDescriptor.AutoCloseInputStream(descriptor).bufferedReader().use { it.readText().take(8192) }
    }

    private fun waitUntil(message: String, timeout: Long = 20000, predicate: () -> Boolean) {
        val end = minOf(deadline, SystemClock.elapsedRealtime() + timeout)
        while (SystemClock.elapsedRealtime() < end) {
            if (predicate()) {
                assertTrue("$message must complete within its bounded deadline", SystemClock.elapsedRealtime() < end)
                return
            }
            Thread.sleep(150)
        }
        fail(message)
    }

    private fun http(method: String, path: String, body: JSONObject? = null, expected: Int = 200, authenticated: Boolean = true): JSONObject {
        check(SystemClock.elapsedRealtime() < deadline) { "Physical case deadline exceeded" }
        val builder = Request.Builder().url(server + path)
        if (authenticated) builder.header("Authorization", "Bearer $admin")
        builder.method(method, body?.toString()?.toRequestBody("application/json".toMediaType()))
        return client.newCall(builder.build()).execute().use { response ->
            assertEquals("HTTP $method $path", expected, response.code)
            val source = response.body?.source()
            assertFalse("Bounded server response required", source?.request(65537) ?: false)
            val text = source?.readUtf8().orEmpty()
            if (text.isEmpty()) JSONObject() else JSONObject(text)
        }
    }

    private fun command(op: String, args: JSONObject = JSONObject(), freshArgs: (() -> JSONObject)? = null): JSONObject {
        val id = credentials?.deviceId ?: error("Real paired device required")
        waitUntil("Production device must advertise $op") {
            val capabilities = http("GET", "/v1/devices/$id").getJSONArray("capabilities")
            (0 until capabilities.length()).any { capabilities.getString(it) == op }
        }
        val dispatchArgs = freshArgs?.invoke() ?: args
        val queued = http("POST", "/v1/devices/$id/commands", JSONObject().put("op", op).put("args", dispatchArgs).put("ttl_seconds", 45).put("idempotency_key", "physical-${UUID.randomUUID()}"))
        var result = JSONObject()
        waitUntil("Production command must reach terminal state") {
            result = http("GET", "/v1/commands/${queued.getString("id")}")
            result.getString("status") in setOf("succeeded", "failed", "expired")
        }
        assertEquals(queued.getString("id"), result.getString("id"))
        assertEquals(id, result.getString("device_id"))
        assertEquals(op, result.getString("op"))
        if (result.getString("status") == "failed") {
            val error = result.getJSONObject("error")
            failure(error.getString("code"), error.getString("message"))
        }
        assertEquals("succeeded", result.getString("status"))
        assertTrue(result.isNull("error"))
        return result.getJSONObject("data")
    }

    private fun stockArgs() = JSONObject().put("package", pkg)

    private fun show(alarm: Boolean) {
        val dispatched = command(if (alarm) "clock.alarms.show" else "clock.timers.show", stockArgs())
        assertEquals(pkg, dispatched.getString("package"))
        assertEquals("native_intent", dispatched.getString("backend"))
        assertFalse(dispatched.getBoolean("verified"))
        waitUntil("Selected installed stock Clock must become foreground") { UiThread.call { BridgeAccessibility.current?.rootInActiveWindow?.packageName?.toString() == pkg } }
        Thread.sleep(300)
        record(if (alarm) "alarms-stock-screen" else "timers-stock-screen")
    }

    private fun list(alarm: Boolean): List<JSONObject> {
        val value = command(if (alarm) "clock.alarms.list" else "clock.timers.list", stockArgs())
        assertEquals("ui", value.getString("backend"))
        assertEquals(pkg, value.getString("package"))
        assertTrue("Readback must cover the complete stock collection", value.getBoolean("complete"))
        assertTrue(value.getBoolean("screen_required"))
        val items = value.getJSONArray(if (alarm) "alarms" else "timers")
        return (0 until items.length()).map { items.getJSONObject(it) }
    }

    private fun selected(packageName: String, value: JSONObject, alarm: Boolean) = JSONObject().put("package", packageName).put("label", value.getString("label")).apply {
        if (alarm) put("time", value.getString("time"))
        if (packageName == "com.sec.android.app.clockpackage") put("state", value.getString("state"))
    }

    private fun sameIdentity(left: JSONObject, right: JSONObject, alarm: Boolean) =
        left.getString("label") == right.getString("label") && (!alarm || left.getString("time") == right.getString("time"))

    private fun currentTarget(values: List<JSONObject>, identity: JSONObject, alarm: Boolean, expectedState: String): JSONObject {
        assertTrue("Explicit requested label is required", identity.getString("label").isNotEmpty())
        val matches = values.filter { sameIdentity(it, identity, alarm) }
        assertEquals("Fresh complete list must uniquely identify the requested label/time", 1, matches.size)
        return matches.single().also {
            assertEquals("Fresh requested state must permit the intended operation", expectedState, it.getString("state"))
            if (alarm) assertEquals(expectedState == "enabled", it.getBoolean("enabled"))
        }
    }

    private fun requiredState(op: String) = when (op) {
        "clock.alarms.disable", "clock.alarms.delete" -> "enabled"
        "clock.alarms.enable" -> "disabled"
        "clock.timers.pause", "clock.timers.cancel" -> "running"
        "clock.timers.resume" -> "paused"
        else -> error("Unexpected physical mutation $op")
    }

    private fun resultingState(op: String): String? = when (op) {
        "clock.alarms.enable" -> "enabled"
        "clock.alarms.disable" -> "disabled"
        "clock.timers.pause" -> "paused"
        "clock.timers.resume" -> "running"
        "clock.alarms.delete", "clock.timers.cancel" -> null
        else -> error("Unexpected physical mutation $op")
    }

    private fun requireClockForeground() {
        UiThread.call {
            val service = BridgeAccessibility.current ?: error("Real Accessibility required")
            Access.unlocked(service)
            service.safeRoot(pkg)
        }
    }

    private fun stableRows(values: List<JSONObject>) = values.map {
        listOf(it.getString("label"), it.optString("time"), it.getString("state"), it.optString("schedule"))
    }.sortedBy { it.toString() }

    private fun mutate(op: String, identity: JSONObject, alarm: Boolean, outcome: String): JSONObject? {
        assertEquals(alarm, op.startsWith("clock.alarms."))
        val desired = resultingState(op)
        assertEquals(if (desired == null) "deleted" else "changed", outcome)
        record("before-$op")
        lateinit var fresh: JSONObject
        var expected = emptyList<JSONObject>()
        val result = command(op, freshArgs = {
            requireClockForeground()
            val before = list(alarm)
            fresh = currentTarget(before, identity, alarm, requiredState(op))
            expected = before.mapNotNull { value ->
                if (!sameIdentity(value, fresh, alarm)) value else desired?.let { state ->
                    JSONObject(value.toString()).put("state", state).apply { if (alarm) put("enabled", state == "enabled") }
                }
            }
            val args = selected(pkg, fresh, alarm)
            requireClockForeground()
            args
        })
        assertEquals("ui", result.getString("backend"))
        assertEquals(pkg, result.getString("package"))
        assertTrue(result.getBoolean("verified"))
        assertTrue(result.getBoolean("screen_required"))
        assertEquals(outcome, result.getString("outcome"))
        assertEquals(desired ?: "absent", result.getString("state"))
        assertEquals(fresh.getString("label"), result.getString("label"))
        if (alarm) assertEquals(fresh.getString("time"), result.getString("time")) else assertTrue(result.isNull("time"))
        var after = emptyList<JSONObject>()
        var matchedAt: Long? = null
        waitUntil("One mutation must have stable fresh complete readback preserving every survivor; no mutation retry", 10000) {
            requireClockForeground()
            after = list(alarm)
            if (stableRows(after) != stableRows(expected)) {
                matchedAt = null
                false
            } else {
                val now = SystemClock.elapsedRealtime()
                if (matchedAt == null) matchedAt = now
                now - matchedAt!! >= 300
            }
        }
        assertEquals(stableRows(expected), stableRows(after))
        val target = if (desired == null) {
            assertFalse(after.any { sameIdentity(it, identity, alarm) })
            null
        } else currentTarget(after, identity, alarm, desired)
        record("after-$op")
        return target
    }

    private fun create(op: String, values: JSONObject, alarm: Boolean, label: String): JSONObject {
        record("before-native-create-$label")
        val dispatched = command(op, values.put("package", pkg))
        assertEquals("dispatched", dispatched.getString("outcome"))
        assertEquals("native_intent", dispatched.getString("backend"))
        assertEquals(pkg, dispatched.getString("package"))
        assertFalse(dispatched.getBoolean("verified"))
        show(alarm)
        var target: JSONObject? = null
        waitUntil("Native create must be confirmed by the source-bound stock adapter") {
            val items = list(alarm)
            target = items.singleOrNull { it.getString("label") == label }
            target != null
        }
        record("after-native-create-$label")
        return target!!
    }

    private fun countdown(label: String): Long = UiThread.call {
        val service = BridgeAccessibility.current ?: error("Real Accessibility required")
        val root = service.safeRoot(pkg)
        val labelId = "$pkg:id/${if (pkg == "com.google.android.deskclock") "timer_label" else "timer_list_timer_name"}"
        val timeId = "$pkg:id/${if (pkg == "com.google.android.deskclock") "timer_text" else "timer_time"}"
        var node = service.nodes(root).single { it.viewIdResourceName == labelId && it.isVisibleToUser && it.text?.toString() == label }
        repeat(24) {
            val children = service.nodes(node)
            val times = children.filter { it.viewIdResourceName == timeId && it.isVisibleToUser }
            if (times.size == 1 && children.count { it.viewIdResourceName == labelId && it.isVisibleToUser } == 1) {
                val text = times.single().text?.toString() ?: error("Stock timer countdown unavailable")
                assertTrue("Source-bound stock timer time format required", text.matches(Regex("(?:[0-9]{1,2}:)?[0-9]{1,2}:[0-5][0-9]")))
                val fields = text.split(':').map { value -> value.toLong() }
                return@call fields.fold(0L) { seconds, value -> seconds * 60 + value }
            }
            node = node.parent ?: error("Source-bound stock timer row unavailable")
        }
        error("Bounded stock timer countdown traversal exceeded")
    }

    private fun packageMetadata(): JSONArray = JSONArray(stockPackages.map { name ->
        try {
            val info = context.packageManager.getPackageInfo(name, PackageManager.GET_SIGNING_CERTIFICATES)
            val currentSigners = info.signingInfo?.apkContentsSigners ?: emptyArray()
            val history = info.signingInfo?.signingCertificateHistory ?: emptyArray()
            fun digest(bytes: ByteArray) = MessageDigest.getInstance("SHA-256").digest(bytes).joinToString("") { "%02x".format(it) }
            val adapter = ClockAdapters(context).forPackage(name)
            JSONObject().put("package", name).put("installed", true).put("version_name", info.versionName ?: "").put("version_code", info.longVersionCode)
                .put("current_signer_sha256", JSONArray(currentSigners.map { digest(it.toByteArray()) })).put("signing_history_sha256", JSONArray(history.map { digest(it.toByteArray()) }))
                .put("adapter", adapter.name).put("supported_operations", JSONArray(adapter.supported(info.versionName ?: "", info.longVersionCode).sorted())).put("support_reason", adapter.reason(info.versionName ?: "", info.longVersionCode))
        } catch (_: PackageManager.NameNotFoundException) {
            JSONObject().put("package", name).put("installed", false)
        }
    })

    private fun record(phase: String) {
        val frame = JSONObject().put("phase", phase).put("monotonic_ms", SystemClock.elapsedRealtime()).put("password_nodes_excluded", true)
        UiThread.call {
            val access = BridgeAccessibility.current
            val root = access?.rootInActiveWindow ?: instrumentation.getUiAutomation(UiAutomation.FLAG_DONT_SUPPRESS_ACCESSIBILITY_SERVICES).rootInActiveWindow
            frame.put("active_package", root?.packageName?.toString() ?: JSONObject.NULL).put("window", root?.windowId ?: -1)
            val nodes = JSONArray()
            var budget = 128000
            fun visit(node: AccessibilityNodeInfo, depth: Int, parent: Int) {
                if (depth > 24 || nodes.length() >= 240 || budget <= 0) { frame.put("truncated", true); return }
                if (node.isPassword || node.packageName?.toString() !in stockPackages) return
                val bounds = Rect().also { node.getBoundsInScreen(it) }
                val index = nodes.length()
                val item = JSONObject().put("index", index).put("parent", parent).put("package", node.packageName?.toString()).put("id", node.viewIdResourceName ?: JSONObject.NULL)
                    .put("class", node.className?.toString()).put("text", node.text?.toString()?.take(256)).put("description", node.contentDescription?.toString()?.take(256))
                    .put("visible", node.isVisibleToUser).put("enabled", node.isEnabled).put("clickable", node.isClickable).put("checkable", node.isCheckable).put("checked", node.isChecked)
                    .put("bounds", JSONArray(listOf(bounds.left, bounds.top, bounds.right, bounds.bottom))).put("actions", JSONArray(node.actionList.take(24).map { it.id }))
                node.collectionInfo?.let { item.put("collection", JSONObject().put("rows", it.rowCount).put("columns", it.columnCount).put("hierarchical", it.isHierarchical).put("selection", it.selectionMode)) }
                node.collectionItemInfo?.let { item.put("item", JSONObject().put("row", it.rowIndex).put("column", it.columnIndex).put("row_span", it.rowSpan).put("column_span", it.columnSpan)) }
                budget -= item.toString().toByteArray().size
                if (budget < 0) { frame.put("truncated", true); return }
                nodes.put(item)
                if (node.childCount > 64) frame.put("truncated", true)
                for (indexChild in 0 until minOf(node.childCount, 64)) node.getChild(indexChild)?.let { child ->
                    try { visit(child, depth + 1, index) } finally { child.recycle() }
                }
            }
            if (root != null) visit(root, 0, -1)
            frame.put("nodes", nodes)
            frame.put("windows", JSONArray(access?.windows?.take(8)?.map { window ->
                JSONObject().put("id", window.id).put("type", window.type).put("active", window.isActive).put("focused", window.isFocused).put("package", window.root?.packageName?.toString())
            } ?: emptyList<JSONObject>()))
        }
        if (frames.length() < 32) frames.put(frame) else frames.put(31, frame)
        save()
    }

    private fun save() {
        report.put("frames", frames)
        val bytes = report.toString().toByteArray()
        check(bytes.size <= 4 * 1024 * 1024) { "Bounded stock evidence exceeded" }
        File(directory, "physical.json").writeBytes(bytes)
        instrumentation.sendStatus(0, Bundle().apply { putString("physicalOemEvidence", File(directory, "physical.json").absolutePath) })
    }

    private fun prepare() {
        deadline = SystemClock.elapsedRealtime() + 480000
        assertEquals("true", arguments.getString("physicalTestLab"))
        assertTrue(BuildConfig.DEBUG)
        assertEquals("true", Settings.System.getString(context.contentResolver, "firebase.test.lab"))
        assertNotEquals("1", shell("getprop ro.kernel.qemu").trim())
        assertNotEquals("1", shell("getprop ro.boot.qemu").trim())
        val fixtureFile = File(context.getExternalFilesDir(null), "phone-oem-fixture.json")
        assertTrue(fixtureFile.isFile && fixtureFile.length() in 1L..131072L)
        val fixture = JSONObject(fixtureFile.readText())
        assertEquals(1, fixture.getInt("schema"))
        assertTrue(fixture.getBoolean("synthetic"))
        binding = fixture.getJSONObject("binding")
        assertEquals("freQuensy23-coder/hermes-phonebridge", binding.getString("repository"))
        for ((key, arg) in listOf("revision" to "revision", "run_id" to "runId", "run_attempt" to "runAttempt", "nonce" to "fixtureNonce")) assertEquals(binding.getString(key), arguments.getString(arg))
        val devices = fixture.getJSONArray("devices")
        assertEquals(2, devices.length())
        val matching = (0 until devices.length()).map { devices.getJSONObject(it) }.single { it.getJSONObject("model").getString("codename") == Build.DEVICE && it.getJSONObject("version").getInt("apiLevel") == Build.VERSION.SDK_INT }
        assertEquals("PHYSICAL", matching.getJSONObject("model").getString("form"))
        assertEquals(matching.getJSONObject("model").getString("manufacturer").lowercase(), Build.MANUFACTURER.lowercase())
        pkg = when (Build.MANUFACTURER.lowercase()) { "samsung" -> "com.sec.android.app.clockpackage"; "google" -> "com.google.android.deskclock"; else -> error("Unexpected physical manufacturer") }
        report.put("binding", binding).put("physical", true).put("device", JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("device", Build.DEVICE).put("sdk", Build.VERSION.SDK_INT).put("android", Build.VERSION.RELEASE).put("fingerprint", Build.FINGERPRINT))
            .put("outcome", "in_progress").put("transport_roundtrip", false).put("revocation_verified", false).put("clock_crud_verified", false)
        report.put("packages", packageMetadata())
        save()
        server = arguments.getString("serverUrl") ?: error("Public HTTPS serverUrl required")
        admin = arguments.getString("adminToken") ?: error("Random synthetic adminToken required")
        assertTrue(server.matches(Regex("https://[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\\.trycloudflare\\.com")))
        assertEquals(server, arguments.getString("expectedPublicServerUrl"))
        assertEquals("$server/", server.toHttpUrl().toString())
        assertTrue(admin.matches(Regex("[A-Za-z0-9_-]{64}")))
        assertEquals("ok", http("GET", "/healthz", authenticated = false).getString("status"))
        assertEquals(binding.getString("revision"), http("GET", "/healthz", authenticated = false).getString("revision"))
        BridgeService.stop(context, true)
        waitUntil("Old service must stop", 5000) { BridgeService.current == null }
        originalServices = Settings.Secure.getString(context.contentResolver, Settings.Secure.ENABLED_ACCESSIBILITY_SERVICES)
        originalAccessibility = Settings.Secure.getString(context.contentResolver, Settings.Secure.ACCESSIBILITY_ENABLED)
        originalTimeFormat = Settings.System.getString(context.contentResolver, Settings.System.TIME_12_24)
        permissionsChanged = true
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        shell("settings put system time_12_24 24")
        shell("settings put secure enabled_accessibility_services $component")
        shell("settings put secure accessibility_enabled 1")
        if (Build.VERSION.SDK_INT >= 33) shell("pm grant org.nous.phonebridge android.permission.POST_NOTIFICATIONS")
        waitUntil("Real Accessibility must connect without suppression") { BridgeAccessibility.current != null }
        Access.unlocked(context)
        report.put("packages", packageMetadata())
        record("before-stock-actions")
    }

    private fun connect() {
        val pairing = http("POST", "/v1/pairings", JSONObject().put("name", "Physical synthetic ${Build.DEVICE}").put("ttl_seconds", 300))
        val qr = PairingQr.parse(pairing.getJSONObject("qr_payload").toString(), false)
        assertEquals("$server/", qr.server.toString())
        val value = BridgeHttp.claim(qr, "Physical CI ${Build.DEVICE}", false)
        credentials = value
        assertEquals("wss://${server.toHttpUrl().host}/v1/device/ws", value.wsUrl)
        Credentials(context).save(value)
        assertEquals(value, Credentials(context).load())
        scenario = ActivityScenario.launch(MainActivity::class.java)
        val previous = context.getSharedPreferences("connection", Context.MODE_PRIVATE).getLong("connections", 0)
        scenario!!.onActivity { BridgeService.start(it) }
        waitUntil("Physical foreground service must connect over public WSS") { http("GET", "/v1/devices/${value.deviceId}").optBoolean("online") && BridgeService.current != null }
        val prefs = context.getSharedPreferences("connection", Context.MODE_PRIVATE)
        assertTrue(prefs.getLong("connections", 0) > previous)
        assertEquals("Connected", prefs.getString("status", ""))
        assertTrue(context.getSystemService(ActivityManager::class.java).getRunningServices(16).any { it.service.className == BridgeService::class.java.name && it.foreground })
        assertTrue(context.getSystemService(NotificationManager::class.java).activeNotifications.any { it.id == 18940 && (it.notification.flags and Notification.FLAG_FOREGROUND_SERVICE) != 0 })
        val info = command("device.info")
        assertEquals(Build.MANUFACTURER, info.getString("manufacturer"))
        assertEquals(Build.MODEL, info.getString("model"))
        assertEquals(Build.VERSION.SDK_INT, info.getInt("sdk"))
        assertEquals(Build.VERSION.RELEASE, info.getString("android"))
        report.put("transport_roundtrip", true)
        save()
    }

    private fun clocks() {
        val info = context.packageManager.getPackageInfo(pkg, 0)
        val adapter = ClockAdapters(context).forPackage(pkg)
        if (!adapter.supported(info.versionName ?: "", info.longVersionCode).containsAll(requiredUi)) {
            val refusedScreens = JSONArray()
            for (alarm in listOf(true, false)) {
                try { show(alarm) } catch (error: Exception) { refusedScreens.put(JSONObject().put("alarm", alarm).put("failure", error.javaClass.name)) } catch (error: AssertionError) { refusedScreens.put(JSONObject().put("alarm", alarm).put("failure", error.javaClass.name)) }
            }
            report.put("unsupported_screen_refusals", refusedScreens)
            record("unsupported-stock-version")
            failure("UNSUPPORTED", "Installed $pkg ${info.versionName} ${info.longVersionCode} has no enabled exact source-bound CRUD profile; stock UI evidence captured")
        }
        show(true)
        val baseline = list(true)
        val suffix = UUID.randomUUID().toString().take(8)
        val label = "PB CI alarm $suffix"
        val future = Calendar.getInstance().apply { add(Calendar.HOUR_OF_DAY, 2) }
        val alarm = create("clock.alarms.create", JSONObject().put("hour", future.get(Calendar.HOUR_OF_DAY)).put("minute", future.get(Calendar.MINUTE)).put("label", label), true, label)
        assertTrue(alarm.getBoolean("enabled"))
        assertTrue("Stock alarm time must match native future time", alarm.getString("time").matches(Regex("0?${future.get(Calendar.HOUR_OF_DAY)}:${future.get(Calendar.MINUTE).toString().padStart(2, '0')}")))
        assertEquals(stableRows(baseline), stableRows(list(true).filter { it.getString("label") != label }))
        val disabledAlarm = mutate("clock.alarms.disable", alarm, true, "changed") ?: error("Disabled alarm readback required")
        assertFalse(list(true).single { it.getString("label") == label }.getBoolean("enabled"))
        assertEquals(stableRows(baseline), stableRows(list(true).filter { it.getString("label") != label }))
        val enabledAlarm = mutate("clock.alarms.enable", disabledAlarm, true, "changed") ?: error("Enabled alarm readback required")
        assertTrue(list(true).single { it.getString("label") == label }.getBoolean("enabled"))
        mutate("clock.alarms.delete", enabledAlarm, true, "deleted")
        assertEquals(stableRows(baseline), stableRows(list(true)))
        show(false)
        val initialTimers = list(false)
        val targetLabel = "PB CI timer $suffix"
        val decoyLabel = "PB CI decoy $suffix"
        val target = create("clock.timers.create", JSONObject().put("seconds", 300).put("label", targetLabel), false, targetLabel)
        assertEquals("running", target.getString("state"))
        assertTrue("Actual stock timer must have the requested 300-second duration", countdown(targetLabel) in 270L..300L)
        val decoy = create("clock.timers.create", JSONObject().put("seconds", 600).put("label", decoyLabel), false, decoyLabel)
        assertEquals("running", decoy.getString("state"))
        val survivors = stableRows(list(false).filter { it.getString("label") != targetLabel })
        assertEquals(stableRows(initialTimers), stableRows(list(false).filter { it.getString("label") !in setOf(targetLabel, decoyLabel) }))
        val pausedTarget = mutate("clock.timers.pause", target, false, "changed") ?: error("Paused requested timer readback required")
        assertEquals("paused", list(false).single { it.getString("label") == targetLabel }.getString("state"))
        val paused = countdown(targetLabel)
        Thread.sleep(1200)
        assertEquals("Requested stock timer must stop counting while paused", paused, countdown(targetLabel))
        assertEquals(survivors, stableRows(list(false).filter { it.getString("label") != targetLabel }))
        val runningTarget = mutate("clock.timers.resume", pausedTarget, false, "changed") ?: error("Resumed requested timer readback required")
        assertEquals("running", list(false).single { it.getString("label") == targetLabel }.getString("state"))
        val resumed = countdown(targetLabel)
        Thread.sleep(1500)
        assertTrue("Requested stock timer must count down after resume", countdown(targetLabel) < resumed)
        assertEquals(survivors, stableRows(list(false).filter { it.getString("label") != targetLabel }))
        mutate("clock.timers.cancel", runningTarget, false, "deleted")
        assertEquals(survivors, stableRows(list(false)))
        assertEquals("running", list(false).single { it.getString("label") == decoyLabel }.getString("state"))
        mutate("clock.timers.cancel", decoy, false, "deleted")
        assertEquals(stableRows(initialTimers), stableRows(list(false)))
        report.put("clock_crud_verified", true)
        record("stock-crud-complete")
    }

    private fun revoke() {
        val value = credentials ?: return
        deadline = maxOf(deadline, SystemClock.elapsedRealtime() + 40000)
        http("DELETE", "/v1/devices/${value.deviceId}")
        waitUntil("Active physical WebSocket revocation must stop the service and erase Keystore credentials") {
            BridgeService.current == null && Credentials(context).load() == null && !context.getSharedPreferences("connection", Context.MODE_PRIVATE).getBoolean("enabled", true)
        }
        val connections = context.getSharedPreferences("connection", Context.MODE_PRIVATE).getLong("connections", -1)
        Thread.sleep(1200)
        assertEquals(connections, context.getSharedPreferences("connection", Context.MODE_PRIVATE).getLong("connections", -1))
        val finished = CountDownLatch(1)
        var rejection = 0
        val socket = client.newWebSocket(Request.Builder().url(value.wsUrl).header("Authorization", "Bearer ${value.token}").build(), object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) { webSocket.cancel(); finished.countDown() }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) { rejection = response?.code ?: 0; finished.countDown() }
        })
        try {
            assertTrue("Revoked token must receive a bounded response", finished.await(12, TimeUnit.SECONDS))
            assertTrue("Revoked physical token must be refused", rejection in setOf(401, 403))
        } finally { socket.cancel() }
        report.put("revocation_verified", true)
        save()
    }

    private fun cleanup(primary: Throwable?, actions: List<() -> Unit>): Throwable? {
        var problem = primary
        for (action in actions) {
            try { action() } catch (error: Throwable) {
                val previous = problem
                if (previous == null) problem = error else if (previous !== error) previous.addSuppressed(error)
            }
        }
        return problem
    }

    private fun settingRestorationCommand(namespace: String, key: String, value: String?): String =
        if (value == null) "settings delete $namespace $key"
        else "settings put $namespace $key '${value.replace("'", "'\\''")}'"

    private fun restoreSetting(namespace: String, key: String, value: String?) {
        shell(settingRestorationCommand(namespace, key, value))
        val restored = when (namespace) {
            "system" -> Settings.System.getString(context.contentResolver, key)
            "secure" -> Settings.Secure.getString(context.contentResolver, key)
            else -> error("Unexpected settings namespace")
        }
        assertEquals("Original $namespace $key must be restored", value, restored)
    }

    private fun selectionControls() {
        assertEquals("running", requiredState("clock.timers.pause"))
        assertEquals("paused", requiredState("clock.timers.resume"))
        assertEquals("running", requiredState("clock.timers.cancel"))
        assertEquals("enabled", requiredState("clock.alarms.disable"))
        assertEquals("disabled", requiredState("clock.alarms.enable"))
        assertEquals("enabled", requiredState("clock.alarms.delete"))
        assertEquals("paused", resultingState("clock.timers.pause"))
        assertEquals("running", resultingState("clock.timers.resume"))
        assertEquals("disabled", resultingState("clock.alarms.disable"))
        assertEquals("enabled", resultingState("clock.alarms.enable"))
        assertNull(resultingState("clock.timers.cancel"))
        assertNull(resultingState("clock.alarms.delete"))
        fun timer(label: String, state: String) = JSONObject().put("label", label).put("state", state).put("time", "00:04:31")
        fun rejected(action: () -> Unit) {
            try { action() } catch (_: AssertionError) { return }
            fail("Invalid fresh selection must fail before any mutation")
        }
        fun bridgeRejected(code: String, action: () -> Unit) {
            try { action() } catch (error: BridgeFailure) { assertEquals(code, error.code); return }
            fail("Adapter selection contract must reject this control")
        }
        val original = timer("requested timer", "running")
        val paused = timer("requested timer", "paused")
        val decoy = timer("decoy timer", "running")
        val fresh = currentTarget(listOf(decoy, paused), original, false, "paused")
        assertSame(paused, fresh)
        assertEquals("running", original.getString("state"))
        val samsung = selected("com.sec.android.app.clockpackage", fresh, false)
        Arguments(samsung).only("package", "id", "label", "time", "state")
        assertEquals("paused", Arguments(samsung).string("state", 16))
        assertFalse("Countdown must never become requested timer identity", samsung.has("time"))
        val entries = listOf(SamsungClockEntry("requested timer", "", "paused"), SamsungClockEntry("decoy timer", "", "running"))
        assertEquals(entries.first(), SamsungClockPolicy.select(entries, samsung.getString("label"), "", samsung.getString("state")))
        bridgeRejected("UNSUPPORTED") {
            val stale = selected("com.sec.android.app.clockpackage", original, false)
            SamsungClockPolicy.select(entries, stale.getString("label"), "", stale.getString("state"))
        }
        bridgeRejected("VALIDATION_ERROR") { Arguments(JSONObject().put("label", original.getString("label"))).string("state", 16) }
        val running = timer("requested timer", "running")
        val cancelled = currentTarget(listOf(decoy, running), paused, false, "running")
        assertSame(running, cancelled)
        assertEquals("running", selected("com.sec.android.app.clockpackage", cancelled, false).getString("state"))
        assertSame(decoy, currentTarget(listOf(decoy), decoy, false, "running"))
        for (value in listOf(fresh, cancelled, decoy)) {
            val google = selected("com.google.android.deskclock", value, false)
            Arguments(google).only("package", "id", "label", "time")
            assertFalse(google.has("state"))
            assertFalse(google.has("time"))
            assertEquals(setOf("package", "label"), google.keys().asSequence().toSet())
            bridgeRejected("VALIDATION_ERROR") { Arguments(JSONObject(google.toString()).put("state", value.getString("state"))).only("package", "id", "label", "time") }
        }
        rejected { currentTarget(listOf(original, decoy), original, false, "paused") }
        rejected { currentTarget(listOf(paused, paused, decoy), original, false, "paused") }
        rejected { currentTarget(listOf(decoy), original, false, "paused") }
        val alarm = JSONObject().put("label", "requested alarm").put("time", "17:31").put("state", "enabled").put("enabled", true)
        val disabled = JSONObject(alarm.toString()).put("state", "disabled").put("enabled", false)
        val otherTime = JSONObject(disabled.toString()).put("time", "19:43")
        val freshAlarm = currentTarget(listOf(otherTime, disabled), alarm, true, "disabled")
        assertSame(disabled, freshAlarm)
        val samsungAlarm = selected("com.sec.android.app.clockpackage", freshAlarm, true)
        assertEquals("disabled", samsungAlarm.getString("state"))
        assertEquals("17:31", samsungAlarm.getString("time"))
        assertEquals(SamsungClockEntry("requested alarm", "17:31", "disabled"), SamsungClockPolicy.select(listOf(SamsungClockEntry("requested alarm", "17:31", "disabled")), samsungAlarm.getString("label"), samsungAlarm.getString("time"), samsungAlarm.getString("state")))
        val googleAlarm = selected("com.google.android.deskclock", freshAlarm, true)
        Arguments(googleAlarm).only("package", "id", "label", "time")
        assertFalse(googleAlarm.has("state"))
        assertEquals("17:31", googleAlarm.getString("time"))
        rejected { currentTarget(listOf(otherTime), alarm, true, "disabled") }
        rejected { currentTarget(listOf(disabled, disabled), alarm, true, "disabled") }
        rejected { currentTarget(listOf(alarm), alarm, true, "disabled") }
    }

    private fun settingRestorationControls() {
        assertEquals("settings delete secure accessibility_enabled", settingRestorationCommand("secure", "accessibility_enabled", null))
        assertEquals("settings put secure accessibility_enabled '0'", settingRestorationCommand("secure", "accessibility_enabled", "0"))
        assertEquals("settings put secure accessibility_enabled ''", settingRestorationCommand("secure", "accessibility_enabled", ""))
        assertEquals("settings put secure accessibility_enabled ' 00'\\''raw '", settingRestorationCommand("secure", "accessibility_enabled", " 00'raw "))
    }

    private fun cleanupControls() {
        val primary = AssertionError("primary mutation failure")
        val evidence = IllegalStateException("evidence failure")
        val close = IllegalStateException("scenario close failure")
        val stop = IllegalStateException("service stop failure")
        val time = IllegalStateException("time restoration failure")
        val services = IllegalStateException("Accessibility services restoration failure")
        val attempted = mutableListOf<String>()
        val actions: List<() -> Unit> = listOf(
            { attempted.add("evidence"); throw evidence },
            { attempted.add("close"); throw close },
            { attempted.add("stop"); throw stop },
            { attempted.add("time"); throw time },
            { attempted.add("services"); throw services },
            { attempted.add("accessibility") }
        )
        assertSame(primary, cleanup(primary, actions))
        assertEquals(listOf("evidence", "close", "stop", "time", "services", "accessibility"), attempted)
        assertEquals(listOf(evidence, close, stop, time, services), primary.suppressed.toList())
        val first = AssertionError("first cleanup failure")
        val second = AssertionError("second cleanup failure")
        assertSame(first, cleanup(null, listOf({ throw first }, { throw second })))
        assertEquals(listOf(second), first.suppressed.toList())
        assertNull(cleanup(null, listOf({ Unit })))
    }

    @Test fun physicalRoundTripNativeClockCrudAndRevocation() {
        var problem: Throwable? = null
        try {
            selectionControls()
            settingRestorationControls()
            cleanupControls()
            prepare()
            connect()
            clocks()
        } catch (error: Throwable) {
            problem = error
        } finally {
            problem = cleanup(problem, listOf(
                { revoke() },
                { report.put("packages_after", packageMetadata()); Unit },
                { record("final-stock-state") },
                { scenario?.close(); Unit },
                { BridgeService.stop(context, true) },
                { if (permissionsChanged) restoreSetting("system", "time_12_24", originalTimeFormat) },
                { if (permissionsChanged) restoreSetting("secure", "enabled_accessibility_services", originalServices) },
                { if (permissionsChanged) restoreSetting("secure", "accessibility_enabled", originalAccessibility) }
            ))
            problem = cleanup(problem, listOf({
                report.put("outcome", if (problem == null) "passed" else if ((problem as? BridgeFailure)?.code == "UNSUPPORTED") "unsupported_failure" else "failed")
                problem?.let { error ->
                    report.put("failure", JSONObject().put("type", error.javaClass.name).put("code", (error as? BridgeFailure)?.code ?: "ASSERTION_OR_INFRASTRUCTURE_FAILURE").put("message", error.message?.take(1024))
                        .put("suppressed", JSONArray(error.suppressed.map { suppressed -> JSONObject().put("type", suppressed.javaClass.name).put("code", (suppressed as? BridgeFailure)?.code ?: "ASSERTION_OR_INFRASTRUCTURE_FAILURE").put("message", suppressed.message?.take(1024)) })))
                }
                save()
            }))
        }
        problem?.let { throw it }
    }
}
