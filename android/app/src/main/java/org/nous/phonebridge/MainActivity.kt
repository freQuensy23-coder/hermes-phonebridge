package org.nous.phonebridge

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.google.zxing.integration.android.IntentIntegrator
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var name: EditText
    private lateinit var debugStatus: TextView
    private var debugDialog: AlertDialog? = null
    private val worker = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private var pairing = false
    private var pairingGeneration = 0L
    private val updater = object : Runnable {
        override fun run() { render(); handler.postDelayed(this, 1000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        val density = resources.displayMetrics.density
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding((20 * density).toInt(), (24 * density).toInt(), (20 * density).toInt(), (24 * density).toInt()) }
        val scroll = ScrollView(this).apply { addView(layout) }
        setContentView(scroll)
        fun text(value: String) { layout.addView(TextView(this).apply { text = value; setPadding(0, 12, 0, 12) }) }
        fun button(label: String, action: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { try { action() } catch (e: BridgeFailure) { message(e.message) } catch (_: Exception) { message("Android refused this action. Open system settings to grant access.") } } }) }
        text("PhoneBridge 0.1.0")
        status = TextView(this).also { layout.addView(it) }
        name = EditText(this).apply { id = R.id.device_name; hint = "Device name"; setText(Build.MODEL); filters = arrayOf(android.text.InputFilter.LengthFilter(80)); isSaveEnabled = false }
        layout.addView(name)
        text("Pair only with a server you trust. Scan the one-time server QR and confirm its hostname. Optional permissions expose the corresponding phone data to that server. Notification text is untrusted content.")
        button("Scan server QR") {
            requireBridge(Credentials(this).load() == null, "Disconnect before pairing a different device")
            if (!Access.has(this, Manifest.permission.CAMERA)) requestPermissions(arrayOf(Manifest.permission.CAMERA), 10) else scan()
        }
        button("Start / resume connection") { BridgeService.start(this); render() }
        button("Stop connection") { pairingGeneration++; pairing = false; BridgeService.stop(this); render() }
        button("Disconnect and erase local token/data") {
            AlertDialog.Builder(this).setTitle("Disconnect?").setMessage("Erase encrypted credentials, command history and queued events. Reconnect requires a new server QR. Revoke the old device on the server to remove its registration.").setNegativeButton("Cancel", null).setPositiveButton("Disconnect") { _, _ -> pairingGeneration++; pairing = false; BridgeService.stop(this, true); render() }.show()
        }
        text("Grant only the permissions you need. SMS access may be restricted by Android's installer or enterprise policy. Permission denial is reported honestly; there is no root, ADB or Shizuku requirement.")
        if (Build.VERSION.SDK_INT >= 33) button("Allow connection notifications") { requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 11) }
        button("Contacts permission") { requestPermissions(arrayOf(Manifest.permission.READ_CONTACTS), 12) }
        button("Calendar permissions") { requestPermissions(arrayOf(Manifest.permission.READ_CALENDAR, Manifest.permission.WRITE_CALENDAR), 13) }
        button("SMS read/send permissions") { requestPermissions(arrayOf(Manifest.permission.READ_SMS, Manifest.permission.SEND_SMS), 14) }
        button("Cached location permission") { requestPermissions(arrayOf(Manifest.permission.ACCESS_COARSE_LOCATION, Manifest.permission.ACCESS_FINE_LOCATION), 15) }
        button("Camera / torch permission") { requestPermissions(arrayOf(Manifest.permission.CAMERA), 16) }
        button("Notification listener access") { startActivity(Intent(Settings.ACTION_NOTIFICATION_LISTENER_SETTINGS)) }
        button("Optional Accessibility screen control") {
            AlertDialog.Builder(this).setTitle("Enable remote screen control?").setMessage("Accessibility can read non-password UI text and perform your server's screen actions. It requires an unlocked, awake phone. Secure-window screenshots are blocked. Google/Samsung clock UI versions without verified selectors are unsupported.").setNegativeButton("Cancel", null).setPositiveButton("Open settings") { _, _ -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }.show()
        }
        text("Experimental remote debug is OFF by default. Accessibility permission alone does not grant debug consent. Each local enable authorizes ordinary screen reading and control by your server for one connection/process lifetime.")
        debugStatus = TextView(this).apply { id = R.id.debug_status }.also { layout.addView(it) }
        fun debugButton(label: String, resourceId: Int, minutes: Int?) {
            layout.addView(Button(this).apply { text = label; id = resourceId; physicalClick(this) { confirmDebug(minutes) } })
        }
        debugButton("Enable debug for 10 minutes", R.id.debug_enable_10, 10)
        debugButton("Enable debug for 15 minutes", R.id.debug_enable_15, 15)
        debugButton("Enable debug for 1 hour", R.id.debug_enable_60, 60)
        debugButton("Enable debug until manually stopped", R.id.debug_enable_manual, null)
        layout.addView(Button(this).apply { text = "Stop debug"; id = R.id.debug_stop; setOnClickListener { DebugAccess.revoke(); render() } })
        button("Do Not Disturb access for audio") { startActivity(Intent(Settings.ACTION_NOTIFICATION_POLICY_ACCESS_SETTINGS)) }
        button("App permissions / battery settings") { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) }
        text("Persistent connection uses a visible foreground service and no permanent wake lock. Android force-stop prevents boot restore until you open the app. Samsung: remove PhoneBridge from sleeping/deep-sleeping apps and select unrestricted battery use. Pixel: select unrestricted battery use. OEM restrictions or network loss can interrupt delivery. Clipboard and activity launches may require PhoneBridge in foreground. Screenshots require Android 14+. No PhoneBridge alarms or timers are created; typed intents dispatch to existing clocks without claiming verification.")
        render()
    }
    private fun physicalClick(button: Button, action: () -> Unit) {
        var touchedAt = -1L
        button.filterTouchesWhenObscured = true
        button.setOnTouchListener { view, event ->
            touchedAt = if (event.actionMasked == android.view.MotionEvent.ACTION_UP && event.isFromSource(android.view.InputDevice.SOURCE_TOUCHSCREEN) && event.flags and (android.view.MotionEvent.FLAG_WINDOW_IS_OBSCURED or android.view.MotionEvent.FLAG_WINDOW_IS_PARTIALLY_OBSCURED) == 0 && view.isShown && view.hasWindowFocus() && resumed) android.os.SystemClock.elapsedRealtime() else -1L
            false
        }
        button.setOnClickListener {
            val physical = touchedAt >= 0 && android.os.SystemClock.elapsedRealtime() - touchedAt <= 1000
            touchedAt = -1L
            if (physical && resumed && button.isShown && button.hasWindowFocus()) {
                try { action() } catch (e: BridgeFailure) { message(e.message) }
            }
        }
    }
    private fun confirmDebug(minutes: Int?) {
        Access.unlocked(this)
        debugDialog?.dismiss()
        val duration = minutes?.let { "$it minutes" } ?: "until manually stopped (this process/connection only)"
        val dialog = AlertDialog.Builder(this).setTitle("Enable experimental remote debug?")
            .setMessage("For $duration your authorized server can read ordinary screen content and operate apps as you. This is high risk and experimental, not ADB or root. It cannot bypass the lock screen, passwords, secure windows or Android permission/credential consent. PhoneBridge consent/settings cannot be remotely controlled. Stop debug here or in the persistent notification at any time. A dispatched gesture may finish after Stop; later commands and late screenshots are blocked. Never authorize payments, sending, deletion or permission grants without your own separate intent.")
            .setNegativeButton("Cancel", null).setPositiveButton("Enable debug", null).create()
        debugDialog = dialog
        dialog.setOnShowListener {
            dialog.window?.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
            physicalClick(dialog.getButton(AlertDialog.BUTTON_POSITIVE)) {
                DebugAccess.enableLocal(this, minutes)
                dialog.dismiss()
                render()
            }
        }
        dialog.show()
    }
    private fun scan() {
        if (pairing) return
        IntentIntegrator(this).setDesiredBarcodeFormats(IntentIntegrator.QR_CODE).setPrompt("Scan the server-generated PhoneBridge pairing QR").setBeepEnabled(false).setOrientationLocked(false).initiateScan()
    }
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        val result = IntentIntegrator.parseActivityResult(requestCode, resultCode, data)
        if (result != null) {
            result.contents?.let { contents ->
                try {
                    val qr = PairingQr.parse(contents, BuildConfig.DEBUG)
                    val deviceName = name.text.toString().trim()
                    requireBridge(deviceName.isNotEmpty(), "Enter a device name")
                    val host = "${qr.server.host}:${qr.server.port}"
                    AlertDialog.Builder(this).setTitle("Trust $host?").setMessage("Server: ${qr.server}\nExpires: ${java.util.Date(qr.expiresAt * 1000)}\n\nThis server will control only the capabilities you grant. Confirm that this hostname belongs to your Hermes host.").setNegativeButton("Cancel", null).setPositiveButton("Pair") { _, _ -> claim(qr, deviceName) }.show()
                } catch (e: BridgeFailure) { message(e.message) } catch (_: Exception) { message("Invalid server QR") }
            }
        } else super.onActivityResult(requestCode, resultCode, data)
    }
    private fun claim(qr: PairingQr, deviceName: String) {
        if (pairing) return
        pairing = true
        val generation = ++pairingGeneration
        status.text = "Claiming one-time pairing…"
        worker.execute {
            try {
                val value = BridgeHttp.claim(qr, deviceName, BuildConfig.DEBUG)
                handler.post {
                    if (!isDestroyed && generation == pairingGeneration) {
                        try {
                            if (Credentials(this).load() != null) failure("PAIRING_FAILED", "A device was already paired; disconnect before replacing it")
                            DurableStore(this).use { it.clearAll() }
                            Credentials(this).save(value)
                            pairing = false
                            BridgeService.start(this)
                            render()
                        } catch (e: Exception) { pairing = false; render(); message((e as? BridgeFailure)?.message ?: "Pairing persistence or foreground startup failed; inspect settings before retrying.") }
                    }
                }
            } catch (e: Exception) {
                handler.post { if (!isDestroyed && generation == pairingGeneration) { pairing = false; render(); message((e as? BridgeFailure)?.message ?: "Pairing failed. A claimed token cannot be reused; generate a fresh QR.") } }
            }
        }
    }
    private fun render() {
        if (!::status.isInitialized || pairing) return
        val debug = DebugAccess.snapshot()
        if (::debugStatus.isInitialized) debugStatus.text = if (!debug.enabled) "Experimental debug: OFF" else "Experimental debug: ON — ${debug.remainingSeconds?.let { "${it}s remaining" } ?: "until manually stopped"}"
        val value = Credentials(this).load()
        val prefs = getSharedPreferences("connection", MODE_PRIVATE)
        status.text = "${prefs.getString("status", "Not connected")}\nServer: ${value?.server ?: "not paired"}\nDevice: ${value?.deviceId ?: "none"}\nGranted: ${Access.permissions(this)}"
    }
    private fun message(value: String) { if (!isFinishing && !isDestroyed) AlertDialog.Builder(this).setMessage(value).setPositiveButton("OK", null).show() }
    override fun onRequestPermissionsResult(requestCode: Int, permissions: Array<out String>, grantResults: IntArray) {
        super.onRequestPermissionsResult(requestCode, permissions, grantResults)
        BridgeService.refresh()
        render()
        if (requestCode == 10 && Access.has(this, Manifest.permission.CAMERA)) scan()
    }
    override fun onResume() {
        super.onResume()
        resumed = true
        BridgeService.refresh()
        handler.post(updater)
    }
    override fun onPause() {
        resumed = false
        debugDialog?.dismiss()
        debugDialog = null
        handler.removeCallbacks(updater)
        BridgeService.refresh()
        super.onPause()
    }
    override fun onDestroy() {
        handler.removeCallbacks(updater)
        worker.shutdown()
        super.onDestroy()
    }
    companion object { @Volatile var resumed = false }
}
