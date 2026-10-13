package org.nous.phonebridge

import android.accessibilityservice.AccessibilityService
import android.content.Context
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.graphics.Rect
import android.os.Build
import android.os.Bundle
import android.os.SystemClock
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.concurrent.CountDownLatch
import java.util.concurrent.TimeUnit

internal class SamsungClockEvidence(private val context: Context, private val test: String) {
    private val run = "$test-${java.util.UUID.randomUUID()}"
    private val directory = File(context.filesDir, "samsung-clock-ci/$run").apply { check(mkdirs() || isDirectory) }
    private val files = mutableSetOf<File>()
    private val frames = JSONArray()
    private var screenshots = 0
    private val pkg = SamsungClockProfile.PACKAGE

    fun inventory(): JSONObject {
        val result = JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("fingerprint", Build.FINGERPRINT).put("sdk", Build.VERSION.SDK_INT).put("profile_status", "source_candidate_runtime_unverified")
        try {
            val info = context.packageManager.getPackageInfo(pkg, PackageManager.GET_SIGNING_CERTIFICATES)
            val app = context.packageManager.getApplicationInfo(pkg, 0)
            result.put("package", pkg).put("version", info.versionName).put("code", info.longVersionCode).put("current_signers", JSONArray(info.signingInfo?.apkContentsSigners?.map { SamsungClockProfile.digest(it.toByteArray()) } ?: emptyList<String>())).put("signer_history", JSONArray(info.signingInfo?.signingCertificateHistory?.map { SamsungClockProfile.digest(it.toByteArray()) } ?: emptyList<String>())).put("apk_path", app.sourceDir).put("apk_bytes", File(app.sourceDir).length()).put("splits", JSONArray(app.splitSourceDirs?.toList() ?: emptyList<String>()))
            val hash = java.security.MessageDigest.getInstance("SHA-256")
            var total = 0L
            File(app.sourceDir).inputStream().use { stream ->
                val buffer = ByteArray(65536)
                while (true) {
                    val count = stream.read(buffer)
                    if (count < 0) break
                    total += count
                    check(total <= 100_000_000L) { "Samsung diagnostic APK exceeds inventory bound" }
                    hash.update(buffer, 0, count)
                }
            }
            result.put("apk_sha256", hash.digest().joinToString("") { "%02x".format(it) })
        } catch (e: Exception) {
            result.put("inventory_error", e.javaClass.simpleName).put("status", "UNSUPPORTED")
        }
        val arguments = InstrumentationRegistry.getArguments()
        result.put("test", test).put("run", run).put("ci_binding", JSONObject().put("revision", arguments.getString("revision")).put("run_id", arguments.getString("runId")).put("run_attempt", arguments.getString("runAttempt")).put("fixture_nonce", arguments.getString("fixtureNonce")))
        val file = File(directory, "inventory.json")
        file.writeText(result.toString())
        files.add(file)
        return result
    }

    fun capture(phase: String, screenshotRequired: Boolean = false) {
        check(frames.length() < 40) { "Samsung CI evidence frame bound exceeded" }
        val frame = JSONObject().put("phase", phase).put("monotonic_ms", SystemClock.elapsedRealtime()).put("bounded", true).put("password_nodes_excluded", true)
        var windowId: Int? = null
        UiThread.call {
            val service = BridgeAccessibility.current ?: return@call
            val active = service.rootInActiveWindow
            val windows = JSONArray()
            var remaining = 400
            var bytes = 0
            if (service.windows.size > 8) frame.put("truncated", true)
            service.windows.take(8).forEach { window ->
                val root = window.root
                val item = JSONObject().put("window", window.id).put("type", window.type).put("active", window.isActive).put("focused", window.isFocused).put("package", root?.packageName?.toString())
                val entries = JSONArray()
                fun visit(node: android.view.accessibility.AccessibilityNodeInfo, parent: Int?, depth: Int) {
                    if (node.isPassword || node.packageName?.toString() != pkg) return
                    if (depth > 30 || remaining <= 0 || bytes >= 200000 || node.childCount > 100) { frame.put("truncated", true); return }
                    val index = entries.length()
                    val rect = Rect().also { node.getBoundsInScreen(it) }
                    val value = JSONObject().put("index", index).put("parent", parent ?: JSONObject.NULL).put("id", node.viewIdResourceName ?: JSONObject.NULL).put("class", node.className?.toString()).put("text", node.text?.toString()?.take(256)).put("description", node.contentDescription?.toString()?.take(256)).put("visible", node.isVisibleToUser).put("enabled", node.isEnabled).put("clickable", node.isClickable).put("long_clickable", node.isLongClickable).put("checkable", node.isCheckable).put("checked", node.isChecked).put("scrollable", node.isScrollable).put("bounds", JSONArray(listOf(rect.left, rect.top, rect.right, rect.bottom))).put("actions", JSONArray(node.actionList.map { it.id })).put("extra_data", JSONArray(node.availableExtraData))
                    node.collectionInfo?.let { value.put("collection", JSONObject().put("rows", it.rowCount).put("columns", it.columnCount).put("hierarchical", it.isHierarchical).put("selection", it.selectionMode)) }
                    node.collectionItemInfo?.let { value.put("item", JSONObject().put("row", it.rowIndex).put("column", it.columnIndex).put("row_span", it.rowSpan).put("column_span", it.columnSpan).put("heading", it.isHeading).put("selected", it.isSelected)) }
                    bytes += value.toString().toByteArray().size
                    if (bytes > 200000) { frame.put("truncated", true); return }
                    entries.put(value)
                    remaining--
                    for (child in 0 until node.childCount) {
                        val next = node.getChild(child)
                        if (next == null) frame.put("truncated", true) else visit(next, index, depth + 1)
                    }
                }
                if (root?.packageName?.toString() == pkg) visit(root, null, 0)
                item.put("nodes", entries)
                windows.put(item)
                if (active?.windowId == window.id && root?.packageName?.toString() == pkg) windowId = window.id
            }
            frame.put("windows", windows)
        }
        if (windowId != null && screenshots < 12 && Build.VERSION.SDK_INT >= 34) {
            val latch = CountDownLatch(1)
            var captured = false
            UiThread.call {
                val service = BridgeAccessibility.current ?: error("Samsung capture service disappeared")
                service.safeRoot(pkg)
                service.takeScreenshotOfWindow(windowId!!, service.mainExecutor, object : AccessibilityService.TakeScreenshotCallback {
                    override fun onSuccess(result: AccessibilityService.ScreenshotResult) {
                        val buffer = result.hardwareBuffer
                        try {
                            Access.unlocked(service)
                            check(service.safeRoot(pkg).windowId == windowId)
                            val original = Bitmap.wrapHardwareBuffer(buffer, result.colorSpace) ?: error("Samsung capture unavailable")
                            try {
                                val scale = minOf(1.0, 1280.0 / maxOf(original.width, original.height))
                                val scaled = Bitmap.createScaledBitmap(original, (original.width * scale).toInt().coerceAtLeast(1), (original.height * scale).toInt().coerceAtLeast(1), true)
                                try {
                                    val output = java.io.ByteArrayOutputStream()
                                    check(scaled.compress(Bitmap.CompressFormat.JPEG, 65, output))
                                    check(output.size() <= 1_000_000)
                                    val file = File(directory, "${frames.length()}-$phase.jpg")
                                    file.writeBytes(output.toByteArray())
                                    files.add(file)
                                    frame.put("screenshot", file.name).put("screenshot_guard", "platform_window_capture")
                                    captured = true
                                } finally { if (scaled !== original) scaled.recycle() }
                            } finally { original.recycle() }
                        } catch (e: Exception) { frame.put("screenshot_error", e.javaClass.simpleName) } finally { buffer.close(); latch.countDown() }
                    }
                    override fun onFailure(errorCode: Int) { frame.put("screenshot_error", errorCode); latch.countDown() }
                })
            }
            check(latch.await(8, TimeUnit.SECONDS)) { "Samsung evidence capture timed out" }
            if (captured) screenshots++
            if (screenshotRequired) check(captured) { "Required Samsung CI screenshot is unavailable" }
        } else if (screenshotRequired) error("Required Samsung window screenshot has no supported active vendor target")
        frames.put(frame)
        flush()
    }

    fun flush() {
        val report = File(directory, "evidence.json")
        report.writeText(JSONObject().put("test", test).put("run", run).put("frames", frames).put("screenshots", screenshots).put("claim", "source_candidate_not_physical_proof_without_passing_positive_tests").toString())
        files.add(report)
        val optIn = InstrumentationRegistry.getArguments().getString("samsungClockFixture")
        var export: File? = null
        if (optIn in setOf("source-12.5.07.13-disposable-physical", "inventory-only-disposable-physical")) {
            export = File(context.getExternalFilesDir(null) ?: error("Samsung CI app-specific export unavailable"), "samsung-clock-ci/$run").apply { check(mkdirs() || isDirectory) }
            check(files.size <= 14 && files.sumOf { it.length() } <= 22_000_000L)
            for (file in files) {
                check(file.parentFile == directory && file.length() <= 9_000_000L)
                file.copyTo(File(export, file.name), overwrite = true)
            }
        }
        InstrumentationRegistry.getInstrumentation().sendStatus(0, Bundle().apply { putString("samsungClockEvidence", report.absolutePath); putString("samsungClockExport", export?.absolutePath) })
    }
}
