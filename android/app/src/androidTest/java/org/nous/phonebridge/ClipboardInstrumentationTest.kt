package org.nous.phonebridge

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
import android.os.SystemClock
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import java.util.concurrent.CountDownLatch
import java.util.concurrent.ExecutionException
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit
import java.util.concurrent.atomic.AtomicInteger

@RunWith(AndroidJUnit4::class)
class ClipboardInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private fun shell(command: String) = instrumentation.uiAutomation.executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { it.readText() } }
    private fun waitUntil(predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 2500
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(10)
        }
        fail("Queued clipboard transition did not settle")
    }
    private fun queuedTransition(op: String, locked: Boolean) {
        BridgeService.stop(context, true)
        waitUntil { BridgeService.current == null }
        shell("input keyevent KEYCODE_WAKEUP")
        shell("wm dismiss-keyguard")
        val scenario = ActivityScenario.launch(MainActivity::class.java)
        val store = DurableStore(context)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val admitted = CountDownLatch(1)
        val guards = AtomicInteger()
        val changes = AtomicInteger()
        var activity: Activity? = null
        val clipboard = context.getSystemService(ClipboardManager::class.java)
        val listener = ClipboardManager.OnPrimaryClipChangedListener { changes.incrementAndGet() }
        val handler = Handler(Looper.getMainLooper())
        var worker: Thread? = null
        try {
            scenario.onActivity {
                activity = it
                Access.unlocked(it)
                assertTrue(MainActivity.resumed)
                clipboard.addPrimaryClipChangedListener(listener)
                clipboard.setPrimaryClip(ClipData.newPlainText("CI", "unchanged baseline"))
            }
            waitUntil { changes.get() > 0 }
            instrumentation.waitForIdleSync()
            changes.set(0)
            handler.post { entered.countDown(); check(release.await(6, TimeUnit.SECONDS)) }
            assertTrue(entered.await(2, TimeUnit.SECONDS))
            val task = FutureTask {
                ExecutionGuard.install(System.currentTimeMillis() / 1000 + 60) {
                    if (Looper.myLooper() != Looper.getMainLooper() && guards.incrementAndGet() == 2) {
                        if (!locked) handler.post { instrumentation.callActivityOnPause(activity!!) }
                        admitted.countDown()
                    }
                    true
                }
                try { NativeHandlers(context, store).execute(op, if (op == "clipboard.set") JSONObject().put("text", "must not be written") else JSONObject()) } finally { ExecutionGuard.clear() }
            }
            worker = Thread(task, "clipboard-queue-regression").also { it.start() }
            assertTrue(admitted.await(2, TimeUnit.SECONDS))
            val running = worker!!
            waitUntil { running.stackTrace.any { it.className == "java.util.concurrent.FutureTask" && it.methodName == "get" } }
            if (locked) {
                shell("input keyevent KEYCODE_SLEEP")
                waitUntil { !context.getSystemService(PowerManager::class.java).isInteractive }
            }
            release.countDown()
            try { task.get(5, TimeUnit.SECONDS); fail("Changed UI admission must refuse clipboard access") } catch (e: ExecutionException) {
                val failure = e.cause as? BridgeFailure ?: throw e
                assertEquals(if (locked) "PHONE_LOCKED" else "FOREGROUND_REQUIRED", failure.code)
            }
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            instrumentation.runOnMainSync { if (!MainActivity.resumed) instrumentation.callActivityOnResume(activity!!) }
            waitUntil { context.getSystemService(PowerManager::class.java).isInteractive && MainActivity.resumed && UiThread.call { activity!!.hasWindowFocus() } }
            instrumentation.waitForIdleSync()
            instrumentation.runOnMainSync {
                assertEquals("unchanged baseline", clipboard.primaryClip!!.getItemAt(0).text.toString())
                assertEquals(0, changes.get())
            }
            assertEquals(2, guards.get())
        } finally {
            release.countDown()
            worker?.join(5000)
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            instrumentation.runOnMainSync {
                clipboard.removePrimaryClipChangedListener(listener)
                activity?.let { if (!MainActivity.resumed) instrumentation.callActivityOnResume(it) }
            }
            scenario.close()
            store.close()
        }
    }

    @Test fun pauseQueuedAfterAdmissionRefusesReadWithoutRetry() = queuedTransition("clipboard.get", false)
    @Test fun pauseQueuedAfterAdmissionRefusesWriteWithoutChangingClipboardOrRetrying() = queuedTransition("clipboard.set", false)
    @Test fun lockWhileMainQueueIsBlockedRefusesReadWithoutRetry() = queuedTransition("clipboard.get", true)
    @Test fun lockWhileMainQueueIsBlockedRefusesWriteWithoutChangingClipboardOrRetrying() = queuedTransition("clipboard.set", true)
}
