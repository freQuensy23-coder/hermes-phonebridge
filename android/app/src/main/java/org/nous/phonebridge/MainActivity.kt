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
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import com.google.zxing.integration.android.IntentIntegrator
import okhttp3.HttpUrl
import java.util.concurrent.Executors

class MainActivity : Activity() {
    private lateinit var status: TextView
    private lateinit var name: EditText
    private lateinit var code: EditText
    private val worker = Executors.newSingleThreadExecutor()
    private val handler = Handler(Looper.getMainLooper())
    private var pairing = false
    private var pairingGeneration = 0L
    private val updater = object : Runnable {
        override fun run() { render(); handler.postDelayed(this, 1000) }
    }
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        val density = resources.displayMetrics.density
        val layout = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL; setPadding((20 * density).toInt(), (24 * density).toInt(), (20 * density).toInt(), (24 * density).toInt()) }
        val scroll = ScrollView(this).apply { addView(layout) }
        val root = android.widget.FrameLayout(this).apply {
            addView(scroll)
            setOnApplyWindowInsetsListener { view, insets ->
                val bars = insets.getInsets(android.view.WindowInsets.Type.systemBars() or android.view.WindowInsets.Type.displayCutout())
                view.setPadding(bars.left, bars.top, bars.right, bars.bottom)
                insets
            }
        }
        setContentView(root)
        fun text(value: String) { layout.addView(TextView(this).apply { text = value; setPadding(0, 12, 0, 12) }) }
        fun button(label: String, action: () -> Unit) { layout.addView(Button(this).apply { text = label; setOnClickListener { try { action() } catch (e: BridgeFailure) { message(e.message) } catch (_: Exception) { message("Android refused this action. Open system settings to grant access.") } } }) }
        text("PhoneBridge ${BuildConfig.VERSION_NAME}")
        status = TextView(this).also { layout.addView(it) }
        name = EditText(this).apply { id = R.id.device_name; hint = "Device name"; setText(Build.MODEL); filters = arrayOf(android.text.InputFilter.LengthFilter(80)); isSaveEnabled = false }
        layout.addView(name)
        text("Pair only with a server you trust. Scan the one-time server QR and confirm its hostname. The server can send files to Download/PhoneBridge and Pictures/PhoneBridge, and once Accessibility is granted it can see and operate the screen.")
        button("Scan server QR") {
            requireBridge(Credentials(this).load() == null, "Disconnect before pairing again")
            if (!Access.has(this, Manifest.permission.CAMERA)) requestPermissions(arrayOf(Manifest.permission.CAMERA), 10) else scan()
        }
        code = EditText(this).apply {
            id = R.id.pairing_code
            hint = "Or paste a connection code (pb1:…)"
            inputType = android.text.InputType.TYPE_CLASS_TEXT or android.text.InputType.TYPE_TEXT_VARIATION_VISIBLE_PASSWORD or android.text.InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            isSingleLine = true
            isSaveEnabled = false
            filters = arrayOf(android.text.InputFilter.LengthFilter(8192))
        }
        layout.addView(code)
        layout.addView(Button(this).apply {
            text = "Connect with code"
            id = R.id.pair_with_code
            setOnClickListener {
                try {
                    requireBridge(Credentials(this@MainActivity).load() == null, "Disconnect before pairing again")
                    confirmPairing(PairingQr.parseInput(code.text.toString(), BuildConfig.DEBUG))
                } catch (e: BridgeFailure) { message(e.message) } catch (_: Exception) { message("Invalid connection code") }
            }
        })
        button("Start / resume connection") { BridgeService.start(this); render() }
        button("Stop connection") { pairingGeneration++; pairing = false; BridgeService.stop(this); render() }
        button("Disconnect and erase local token/data") {
            AlertDialog.Builder(this).setTitle("Disconnect?").setMessage("Erase encrypted credentials, command history and queued events. Reconnect requires a new server QR. Revoke the old device on the server to remove its registration.").setNegativeButton("Cancel", null).setPositiveButton("Disconnect") { _, _ -> pairingGeneration++; pairing = false; BridgeService.stop(this, true); render() }.show()
        }
        if (Build.VERSION.SDK_INT >= 33) button("Allow notifications") { requestPermissions(arrayOf(Manifest.permission.POST_NOTIFICATIONS), 11) }
        button("Grant Accessibility (screen control)") {
            AlertDialog.Builder(this).setTitle("Grant Accessibility?").setMessage("PhoneBridge uses Accessibility to read and operate the screen for your server. If Android shows \"Restricted setting\", open App info, tap the menu and allow restricted settings first.").setNegativeButton("Cancel", null).setPositiveButton("Open settings") { _, _ -> startActivity(Intent(Settings.ACTION_ACCESSIBILITY_SETTINGS)) }.show()
        }
        button("App permissions / battery settings") { startActivity(Intent(Settings.ACTION_APPLICATION_DETAILS_SETTINGS, Uri.parse("package:$packageName"))) }
        text("Persistent connection uses a visible foreground service and no permanent wake lock. Android force-stop prevents boot restore until you open the app. Samsung: remove PhoneBridge from sleeping/deep-sleeping apps and select unrestricted battery use. Pixel: select unrestricted battery use. OEM restrictions or network loss can interrupt delivery.")
        render()
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
        val generation = ++pairingGeneration
        status.text = "Claiming one-time pairing…"
        val appContext = applicationContext
        worker.execute {
            try {
                val value = BridgeHttp.claim(qr, deviceName, BuildConfig.DEBUG)
                synchronized(MainActivity) {
                    if (Credentials(appContext).load() != null) failure("PAIRING_FAILED", "A device was already paired; disconnect before replacing it")
                    DurableStore(appContext).use { it.clearAll() }
                    Credentials(appContext).save(value)
                }
                handler.post {
                    pairing = false
                    if (!isDestroyed) {
                        try { BridgeService.start(this); render() } catch (e: Exception) { render(); message((e as? BridgeFailure)?.message ?: "Paired, but the connection did not start; tap Start / resume connection.") }
                    }
                }
            } catch (e: Exception) {
                handler.post { if (!isDestroyed && generation == pairingGeneration) { pairing = false; render(); message((e as? BridgeFailure)?.message ?: "Pairing failed. A claimed token cannot be reused; generate a fresh QR.") } }
            }
        }
    }
    private fun render() {
        if (!::status.isInitialized || pairing) return
        val value = try { Credentials(this).load() } catch (_: Exception) { null }
        val prefs = getSharedPreferences("connection", MODE_PRIVATE)
        val permissions = Access.permissions(this)
        fun mark(key: String) = if (permissions.optBoolean(key)) "yes" else "no"
        status.text = "Status: ${prefs.getString("status", "Not connected")}\nServer: ${value?.server?.removeSuffix("/") ?: "not paired"}\nAccessibility granted: ${mark("accessibility")}\nNotifications allowed: ${mark("post_notifications")}"
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
        worker.shutdown()
        super.onDestroy()
    }
    companion object { @Volatile var resumed = false }
}
