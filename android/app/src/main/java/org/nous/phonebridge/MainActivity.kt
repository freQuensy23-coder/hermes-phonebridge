package org.nous.phonebridge

import android.Manifest
import android.app.Activity
import android.app.AlertDialog
import android.content.Context
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.provider.Settings
import android.widget.EditText
import android.widget.TextView
import com.google.zxing.integration.android.IntentIntegrator
import okhttp3.HttpUrl
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var name: EditText
    private lateinit var code: EditText
    private val handler = Handler(Looper.getMainLooper())
    private val updater = object : Runnable {
        override fun run() { render(); handler.postDelayed(this, 1000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setDecorFitsSystemWindows(false)
        window.statusBarColor = android.graphics.Color.rgb(245, 247, 246)
        window.navigationBarColor = android.graphics.Color.rgb(245, 247, 246)

        window.addFlags(android.view.WindowManager.LayoutParams.FLAG_SECURE)
        ui = OwnerUi(this, ::action)
        setContentView(ui.root)
        window.insetsController?.setSystemBarsAppearance(
            android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS,
            android.view.WindowInsetsController.APPEARANCE_LIGHT_STATUS_BARS or android.view.WindowInsetsController.APPEARANCE_LIGHT_NAVIGATION_BARS
        )
        status = ui.status
        name = ui.name
        code = ui.code
        visible = this
        render()
    }
    private lateinit var ui: OwnerUi
    private fun action(key: String) {
        try {
            when (key) {
                "scan" -> {
                    requireBridge(Credentials(this).load() == null, "Disconnect before pairing again")
                    if (!Access.has(this, Manifest.permission.CAMERA)) requestPermissions(arrayOf(Manifest.permission.CAMERA), 10) else scan()
                }
                "pair" -> {
                    requireBridge(Credentials(this).load() == null, "Disconnect before pairing again")
                    confirmPairing(PairingQr.parseInput(code.text.toString(), BuildConfig.DEBUG))
                }
                "cancel_pair" -> cancelPairing()
                "connection" -> {
                    if (getSharedPreferences("connection", MODE_PRIVATE).getBoolean("enabled", false)) {
                        cancelPairing(); BridgeService.stop(this)
                    } else BridgeService.start(this)
                }
                "disconnect" -> AlertDialog.Builder(this).setTitle("Forget this server?")
                    .setMessage("This stops remote access and erases credentials and command history on this phone. You will need a new connection code. To delete its server registration too, run phonebridge devices revoke DEVICE on the server.")
                    .setNegativeButton("Keep server", null).setPositiveButton("Forget server") { _, _ -> cancelPairing(); BridgeService.stop(this, true); render() }.show()
                "notifications" -> if (Build.VERSION.SDK_INT >= 33) requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 11)
                "accessibility" -> AlertDialog.Builder(this).setTitle("Allow screen access in Android")
                    .setMessage("Your server can read and operate other apps while screen control is on. Enable PhoneBridge in Accessibility settings, then return here to choose a duration.\n\nSamsung: if blocked, open App info → ⋮ → Allow restricted settings first.")
                    .setNegativeButton("Cancel", null).setPositiveButton("Open settings") { _, _ -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }.show()
                "10", "15", "60", "manual" -> {
                    requireBridge(Credentials(this).load() != null, "Connect your server first")
                    requireBridge(BridgeAccessibility.current != null, "Enable PhoneBridge Accessibility first")
                    if (key == "manual") AlertDialog.Builder(this).setTitle("Allow until you stop it?")
                        .setMessage("Your server will keep screen access without a time limit, including after reconnecting. You can end access here or stop the connection from the notification.")
                        .setNegativeButton("Cancel", null).setPositiveButton("Allow until stopped") { _, _ -> ScreenControl.enable(this, null); render() }.show()
                    else ScreenControl.enable(this, key.toInt())
                }
                "screen_stop" -> ScreenControl.stop(this)
                "files" -> startActivity(Intent(android.app.DownloadManager.ACTION_VIEW_DOWNLOADS))
                "settings" -> startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName")))
                "help" -> AlertDialog.Builder(this).setTitle("Keep the connection available")
                    .setMessage("Set PhoneBridge battery use to Unrestricted. On Samsung, also remove it from Sleeping and Deep sleeping apps.\n\nForce-stop blocks automatic reconnect until you reopen the app. Internet loss pauses delivery; PhoneBridge retries when the network returns.")
                    .setPositiveButton("App settings") { _, _ -> action("settings") }.setNegativeButton("Close", null).show()
            }
            render()
        } catch (e: BridgeFailure) { message(e.message) }
          catch (_: Exception) { message("Android could not open this action. Check app permissions in Settings.") }
    }
    private fun cancelPairing() {
        synchronized(MainActivity) {
            pairingGeneration++
            if (pairing) BridgeService.stop(this, true)
            pairing = false
        }
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
                    confirmPairing(PairingQr.parseInput(contents, BuildConfig.DEBUG))
                } catch (e: BridgeFailure) { message(e.message) } catch (_: Exception) { message("Invalid server QR") }
            }
        } else super.onActivityResult(requestCode, resultCode, data)
    }
    private fun confirmPairing(qr: PairingQr) {
        val deviceName = name.text.toString().trim()
        requireBridge(deviceName.isNotEmpty(), "Enter a device name")
        val host = if (qr.server.port == HttpUrl.defaultPort(qr.server.scheme)) qr.server.host else "${qr.server.host}:${qr.server.port}"
        AlertDialog.Builder(this).setTitle("Trust $host?").setMessage("Server: ${qr.server}\nCode expires: ${java.text.DateFormat.getTimeInstance(java.text.DateFormat.SHORT).format(java.util.Date(qr.expiresAt * 1000))}\n\nPair only if this is your own Hermes server. It will be able to send you files and see and operate the screen.")
            .setNegativeButton("Cancel", null).setPositiveButton("Pair") { _, _ -> code.text.clear(); claim(qr, deviceName) }.show()
    }
    private fun claim(qr: PairingQr, deviceName: String) {
        if (pairing) return
        pairing = true
        val generation = synchronized(MainActivity) { ++pairingGeneration }
        status.text = "Connecting to server…"
        ui.pairing(true)
        val appContext = applicationContext
        worker.execute {
            val failed = try {
                val value = BridgeHttp.claim(qr, deviceName, BuildConfig.DEBUG)
                synchronized(MainActivity) {
                    if (generation != pairingGeneration) return@execute
                    if (Credentials(appContext).load() != null) failure("PAIRING_FAILED", "A device was already paired; disconnect before replacing it")
                    DurableStore(appContext).use { it.clearAll() }
                    Credentials(appContext).save(value)
                }
                null
            } catch (e: Exception) {
                (e as? BridgeFailure)?.message ?: "Pairing failed. A claimed token cannot be reused; generate a fresh QR."
            }
            mainHandler.post { finishPairing(appContext, generation, failed) }
        }
    }
    private fun render() {
        if (!::status.isInitialized) return
        if (pairing) {
            status.text = "Connecting to server…"
            ui.pairing(true)
            return
        }
        ui.pairing(false)
        val value = try { Credentials(this).load() } catch (_: Exception) { null }
        ui.render(value, Access.permissions(this), getSharedPreferences("connection", MODE_PRIVATE), ScreenControl.remaining(this))
        pendingMessage?.let { pendingMessage = null; message(it) }
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
        handler.removeCallbacks(updater)
        BridgeService.refresh()
        super.onPause()
    }
    override fun onDestroy() {
        handler.removeCallbacks(updater)
        if (visible === this) visible = null
        if (isFinishing && !isChangingConfigurations) cancelPairing()
        super.onDestroy()
    }
    companion object {
        @Volatile var resumed = false
        private val worker = Executors.newSingleThreadExecutor()
        private val mainHandler = Handler(Looper.getMainLooper())
        @Volatile private var pairing = false
        @Volatile private var pairingGeneration = 0L
        private var pendingMessage: String? = null
        private var visible: MainActivity? = null

        private fun finishPairing(context: Context, generation: Long, failed: String?) {
            if (generation != pairingGeneration) return
            pairing = false
            val problem = failed ?: try {
                BridgeService.start(context)
                null
            } catch (e: Exception) {
                (e as? BridgeFailure)?.message ?: "Paired, but the connection did not start; tap Resume connection."
            }
            val activity = visible
            if (problem != null) pendingMessage = problem
            activity?.render()
        }
    }
}
