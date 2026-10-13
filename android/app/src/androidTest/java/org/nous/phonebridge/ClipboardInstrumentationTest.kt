package org.nous.phonebridge

import android.app.Activity
import android.content.ClipData
import android.content.ClipboardManager
import android.content.Context
import android.content.ContextWrapper
import android.os.Handler
import android.os.Looper
import android.os.PowerManager
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
    @get:org.junit.Rule val testName = org.junit.rules.TestName()
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private class ClipboardAccessContext(base: Context) : ContextWrapper(base) {
        val clipboardRequests = AtomicInteger()
        override fun getSystemService(name: String): Any? {
            if (name == Context.CLIPBOARD_SERVICE) clipboardRequests.incrementAndGet()
            return super.getSystemService(name)
        }
    }
    private fun shell(command: String) = ForegroundFixture.shell(command)
    private fun waitUntil(phase: String, predicate: () -> Boolean) = ForegroundFixture.waitUntil("${testName.methodName}-$phase", 2500, predicate)
    private fun queuedTransition(op: String, locked: Boolean) {
        BridgeService.stop(context, true)
        waitUntil("service-stopped") { BridgeService.current == null }
        ForegroundFixture.prepare(testName.methodName)
        val scenario = ActivityScenario.launch(MainActivity::class.java)
        val store = DurableStore(context)
        val entered = CountDownLatch(1)
        val release = CountDownLatch(1)
        val admitted = CountDownLatch(1)
        val guards = AtomicInteger()
        val uiGuards = AtomicInteger()
        val handlerContext = ClipboardAccessContext(context)
        var activity: Activity? = null
        val clipboard = context.getSystemService(ClipboardManager::class.java)
        val handler = Handler(Looper.getMainLooper())
        var worker: Thread? = null
        try {
            waitUntil("activity-focused-before-baseline") { ForegroundFixture.focused() }
            scenario.onActivity {
                activity = it
                Access.unlocked(it)
                assertTrue(MainActivity.resumed)
                clipboard.setPrimaryClip(ClipData.newPlainText("CI", "unchanged baseline"))
            }
            waitUntil("focused-clipboard-baseline") { ForegroundFixture.focused() && UiThread.call { clipboard.primaryClip?.getItemAt(0)?.text?.toString() == "unchanged baseline" } }
            instrumentation.waitForIdleSync()
            assertEquals(0, handlerContext.clipboardRequests.get())
            handler.post { entered.countDown(); check(release.await(6, TimeUnit.SECONDS)) }
            assertTrue(entered.await(2, TimeUnit.SECONDS))
            val task = FutureTask {
                ExecutionGuard.install(System.currentTimeMillis() / 1000 + 60) {
                    if (Looper.myLooper() == Looper.getMainLooper()) {
                        uiGuards.incrementAndGet()
                    } else if (guards.incrementAndGet() == 2) {
                        if (!locked) handler.post { instrumentation.callActivityOnPause(activity!!) }
                        admitted.countDown()
                    }
                    true
                }
                try { NativeHandlers(handlerContext, store).execute(op, if (op == "clipboard.set") JSONObject().put("text", "must not be written") else JSONObject()) } finally { ExecutionGuard.clear() }
            }
            worker = Thread(task, "clipboard-queue-regression").also { it.start() }
            assertTrue(admitted.await(2, TimeUnit.SECONDS))
            val running = worker!!
            waitUntil("effect-queued-behind-transition") { running.stackTrace.any { it.className == "java.util.concurrent.FutureTask" && it.methodName == "get" } }
            if (locked) {
                shell("input keyevent KEYCODE_SLEEP")
                waitUntil("screen-asleep") { !context.getSystemService(PowerManager::class.java).isInteractive }
            }
            release.countDown()
            try { task.get(5, TimeUnit.SECONDS); fail("Changed UI admission must refuse clipboard access") } catch (e: ExecutionException) {
                val failure = e.cause as? BridgeFailure ?: throw e
                assertEquals(if (locked) "PHONE_LOCKED" else "FOREGROUND_REQUIRED", failure.code)
            }
            assertEquals(0, handlerContext.clipboardRequests.get())
            assertEquals(1, uiGuards.get())
            shell("input keyevent KEYCODE_WAKEUP")
            shell("wm dismiss-keyguard")
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.CREATED)
            scenario.moveToState(androidx.lifecycle.Lifecycle.State.RESUMED)
            waitUntil("activity-focused-after-refusal") { ForegroundFixture.focused() }
            instrumentation.waitForIdleSync()
            instrumentation.runOnMainSync {
                assertEquals("unchanged baseline", clipboard.primaryClip!!.getItemAt(0).text.toString())
            }
            assertEquals(0, handlerContext.clipboardRequests.get())
            assertEquals(1, uiGuards.get())
            assertEquals(2, guards.get())
        } finally {
            release.countDown()
            worker?.join(5000)
            try {
                ForegroundFixture.wake()
                if (worker?.isAlive != true && ForegroundFixture.focused()) instrumentation.runOnMainSync { clipboard.clearPrimaryClip() }
            } finally {
                try { scenario.close() } finally { store.close() }
            }
        }
    }

    @Test fun foregroundReadAndWriteReachTheRealClipboardAndVerifyText() {
        BridgeService.stop(context, true)
        waitUntil("service-stopped") { BridgeService.current == null }
        ForegroundFixture.prepare(testName.methodName)
        val scenario = ActivityScenario.launch(MainActivity::class.java)
        try {
            var activity: Activity? = null
            scenario.onActivity { activity = it }
            waitUntil("positive-activity-focused") { ForegroundFixture.focused() && UiThread.call { activity!!.hasWindowFocus() } }
            val clipboard = context.getSystemService(ClipboardManager::class.java)
            instrumentation.runOnMainSync { clipboard.setPrimaryClip(ClipData.newPlainText("CI", "foreground read baseline")) }
            DurableStore(context).use { store ->
                val handlerContext = ClipboardAccessContext(context)
                val handlers = NativeHandlers(handlerContext, store)
                val read = handlers.execute("clipboard.get", JSONObject())
                assertEquals("foreground read baseline", read.getString("text"))
                assertTrue(read.getBoolean("verified"))
                assertEquals(1, handlerContext.clipboardRequests.get())
                val written = handlers.execute("clipboard.set", JSONObject().put("text", "foreground write verified"))
                assertEquals("foreground write verified", written.getString("text"))
                assertTrue(written.getBoolean("verified"))
                assertEquals(2, handlerContext.clipboardRequests.get())
                instrumentation.runOnMainSync { assertEquals("foreground write verified", clipboard.primaryClip!!.getItemAt(0).text.toString()) }
            }
        } finally {
            try {
                if (ForegroundFixture.focused()) instrumentation.runOnMainSync { context.getSystemService(ClipboardManager::class.java).clearPrimaryClip() }
            } finally { scenario.close() }
        }
    }

    @Test fun pauseQueuedAfterAdmissionRefusesReadWithoutRetry() = queuedTransition("clipboard.get", false)
    @Test fun pauseQueuedAfterAdmissionRefusesWriteWithoutChangingClipboardOrRetrying() = queuedTransition("clipboard.set", false)
    @Test fun lockWhileMainQueueIsBlockedRefusesReadWithoutRetry() = queuedTransition("clipboard.get", true)
    @Test fun lockWhileMainQueueIsBlockedRefusesWriteWithoutChangingClipboardOrRetrying() = queuedTransition("clipboard.set", true)
}
