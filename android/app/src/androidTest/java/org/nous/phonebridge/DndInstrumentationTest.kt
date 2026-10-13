package org.nous.phonebridge

import android.Manifest
import android.app.NotificationManager
import android.content.pm.PackageManager
import android.media.AudioManager
import android.os.SystemClock
import android.view.View
import android.view.ViewGroup
import android.widget.Button
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class DndInstrumentationTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val context = instrumentation.targetContext
    private val listener = "org.nous.phonebridge/org.nous.phonebridge.BridgeNotifications"
    private fun shell(command: String) = instrumentation.uiAutomation.executeShellCommand(command).use { parcel -> android.os.ParcelFileDescriptor.AutoCloseInputStream(parcel).bufferedReader().use { it.readText() } }
    private fun waitUntil(predicate: () -> Boolean) {
        val deadline = SystemClock.elapsedRealtime() + 5000
        while (SystemClock.elapsedRealtime() < deadline) {
            if (predicate()) return
            Thread.sleep(100)
        }
        fail("Permission state did not settle")
    }
    private fun views(root: View): List<View> = listOf(root) + if (root is ViewGroup) (0 until root.childCount).flatMap { views(root.getChildAt(it)) } else emptyList()

    @Test fun independentDndOnboardingGrantAndRevokeControlRingAndNotificationWithoutListener() {
        BridgeService.stop(context, true)
        waitUntil { BridgeService.current == null }
        val manager = context.getSystemService(NotificationManager::class.java)
        val audio = context.getSystemService(AudioManager::class.java)
        val volumes = mapOf("ring" to audio.getStreamVolume(AudioManager.STREAM_RING), "notification" to audio.getStreamVolume(AudioManager.STREAM_NOTIFICATION))
        shell("cmd notification disallow_listener $listener")
        shell("cmd notification disallow_dnd ${context.packageName}")
        shell("cmd notification set_dnd off")
        waitUntil { BridgeNotifications.current == null && !manager.isNotificationPolicyAccessGranted }
        val requested = context.packageManager.getPackageInfo(context.packageName, PackageManager.GET_PERMISSIONS).requestedPermissions.orEmpty()
        assertTrue(Manifest.permission.ACCESS_NOTIFICATION_POLICY in requested)
        val scenario = ActivityScenario.launch(MainActivity::class.java)
        try {
            scenario.onActivity { activity ->
                val button = views(activity.window.decorView).filterIsInstance<Button>().single { it.text.toString() == "Do Not Disturb access for audio" }
                assertTrue(button.performClick())
            }
            waitUntil { !MainActivity.resumed }
            DurableStore(context).use { store ->
                val handlers = NativeHandlers(context, store)
                fun refused() {
                    val permissions = Access.permissions(context)
                    assertFalse(permissions.getBoolean("notification_policy"))
                    assertFalse(permissions.getBoolean("notification_listener"))
                    assertTrue(Access.capabilities(context).toString().contains("audio.set"))
                    for ((name, stream) in listOf("ring" to AudioManager.STREAM_RING, "notification" to AudioManager.STREAM_NOTIFICATION)) {
                        val before = audio.getStreamVolume(stream)
                        val volume = if (before == 1) 2 else 1
                        assertTrue(audio.getStreamMaxVolume(stream) >= volume)
                        try { handlers.execute("audio.set", JSONObject().put("stream", name).put("volume", volume)); fail("DND grant must be required") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
                        assertEquals(before, audio.getStreamVolume(stream))
                    }
                }
                refused()
                shell("cmd notification allow_dnd ${context.packageName}")
                waitUntil { manager.isNotificationPolicyAccessGranted }
                assertTrue(Access.permissions(context).getBoolean("notification_policy"))
                assertFalse(Access.permissions(context).getBoolean("notification_listener"))
                for ((name, stream) in listOf("ring" to AudioManager.STREAM_RING, "notification" to AudioManager.STREAM_NOTIFICATION)) {
                    val before = audio.getStreamVolume(stream)
                    val volume = if (before == 1) 2 else 1
                    val result = handlers.execute("audio.set", JSONObject().put("stream", name).put("volume", volume))
                    assertTrue(result.getBoolean("verified"))
                    assertEquals(volume, result.getInt("requested_volume"))
                    assertEquals(volume, audio.getStreamVolume(stream))
                }
                shell("cmd notification disallow_dnd ${context.packageName}")
                waitUntil { !manager.isNotificationPolicyAccessGranted }
                refused()
                val music = audio.getStreamVolume(AudioManager.STREAM_MUSIC)
                assertTrue(handlers.execute("audio.set", JSONObject().put("stream", "music").put("volume", music)).getBoolean("verified"))
            }
        } finally {
            shell("cmd notification allow_dnd ${context.packageName}")
            waitUntil { manager.isNotificationPolicyAccessGranted }
            for ((name, volume) in volumes) audio.setStreamVolume(NativeHandlers.streams.getValue(name), volume, 0)
            shell("cmd notification disallow_dnd ${context.packageName}")
            scenario.close()
        }
    }
}
