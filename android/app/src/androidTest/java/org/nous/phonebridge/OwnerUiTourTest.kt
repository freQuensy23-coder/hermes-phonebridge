package org.nous.phonebridge

import android.content.Context
import android.os.ParcelFileDescriptor
import android.view.View
import android.view.WindowManager
import androidx.test.core.app.ActivityScenario
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Assume.assumeTrue
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class OwnerUiTourTest {
    private val instrumentation = InstrumentationRegistry.getInstrumentation()
    private val automation get() = instrumentation.uiAutomation
    private val directory = "/sdcard/Download/phonebridge-redesign"
    private fun shell(command: String) {
        ParcelFileDescriptor.AutoCloseInputStream(automation.executeShellCommand(command)).use { it.readBytes() }
    }
    private fun capture(name: String) {
        instrumentation.waitForIdleSync()
        Thread.sleep(600)
        shell("screencap -p $directory/$name.png")
    }
    @Test fun captureSetupConnectedActiveAndPaused() {
        assumeTrue(InstrumentationRegistry.getArguments().getString("ownerUiTour") == "true")
        shell("mkdir -p $directory")
        val context = instrumentation.targetContext
        val prefs = context.getSharedPreferences("owner_ui_tour", Context.MODE_PRIVATE)
        val credentials = DeviceCredentials("screenshot-phone", "synthetic-not-a-real-token", "https://hermes.example.com/", "wss://hermes.example.com/v1/device/ws")
        val permissions = JSONObject().put("accessibility", true).put("post_notifications", true)
        val events = mutableListOf<String>()
        try {
            ActivityScenario.launch(MainActivity::class.java).use { scenario ->
                lateinit var ui: OwnerUi
                scenario.onActivity { activity ->
                    activity.window.clearFlags(WindowManager.LayoutParams.FLAG_SECURE)
                    ui = OwnerUi(activity) { events.add(it) }
                    activity.setContentView(ui.root)
                    ui.render(null, permissions, prefs, null)
                    ui.name.setText("My Android")
                    assertEquals(View.VISIBLE, ui.code.parent.let { it as View }.visibility)
                }
                capture("01-setup")
                prefs.edit().putBoolean("enabled", true).putString("status", "Connected").commit()
                scenario.onActivity {
                    ui.render(credentials, permissions, prefs, null)
                    assertFalse(ui.code.isShown)
                    assertTrue(ui.root.findViewById<View>(R.id.screen_enable_10).isEnabled)
                    ui.root.findViewById<View>(R.id.screen_enable_10).performClick()
                    assertEquals(listOf("10"), events)
                }
                capture("02-connected")
                scenario.onActivity { ui.render(credentials, permissions, prefs, 9 * 60000L + 42000L) }
                capture("03-screen-active")
                prefs.edit().putBoolean("enabled", false).putString("status", "Stopped").commit()
                scenario.onActivity {
                    ui.render(credentials, permissions, prefs, null)
                    assertFalse(ui.root.findViewById<View>(R.id.screen_enable_10).isEnabled)
                    assertEquals("Connection paused", ui.status.text.toString())
                }
                capture("04-paused")
            }
        } finally { prefs.edit().clear().commit() }
    }
}
