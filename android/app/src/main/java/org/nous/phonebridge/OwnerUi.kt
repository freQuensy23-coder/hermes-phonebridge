package org.nous.phonebridge

import android.app.Activity
import android.content.SharedPreferences
import android.graphics.Color
import android.graphics.Typeface
import android.graphics.drawable.GradientDrawable
import android.os.Build
import android.text.InputFilter
import android.text.InputType
import android.view.View
import android.view.WindowInsets
import android.widget.*
import org.json.JSONObject

/** Native views shared by the setup and daily-use flows. No network or permission side effects. */
class OwnerUi(private val activity: Activity, private val action: (String) -> Unit) {
    private val ink = Color.rgb(18, 35, 44)
    private val muted = Color.rgb(91, 108, 115)
    private val accent = Color.rgb(0, 105, 91)
    private val pale = Color.rgb(229, 242, 237)
    private val paper = Color.rgb(245, 247, 246)
    private val line = Color.rgb(219, 227, 224)
    private val danger = Color.rgb(167, 48, 52)
    private fun dp(n: Int) = (n * activity.resources.displayMetrics.density).toInt()
    private fun column() = LinearLayout(activity).apply { orientation = LinearLayout.VERTICAL }
    private fun shape(color: Int, radius: Int = 20, stroke: Boolean = false) = GradientDrawable().apply {
        setColor(color); cornerRadius = dp(radius).toFloat(); if (stroke) setStroke(dp(1), line)
    }
    private fun label(text: String, size: Float = 15f, color: Int = ink, bold: Boolean = false) = TextView(activity).apply {
        this.text = text; textSize = size; setTextColor(color); setLineSpacing(dp(3).toFloat(), 1f)
        if (bold) typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setPadding(0, dp(4), 0, dp(4))
    }
    private fun button(text: String, key: String, primary: Boolean = false, id: Int = View.NO_ID) = Button(activity).apply {
        this.text = text; this.id = id; isAllCaps = false; textSize = 15f
        typeface = Typeface.create("sans-serif-medium", Typeface.NORMAL)
        setTextColor(if (primary) Color.WHITE else accent)
        background = shape(if (primary) accent else pale, 14)
        minHeight = dp(48); minimumHeight = dp(48); setPadding(dp(12), dp(8), dp(12), dp(8))
        stateListAnimator = null
        layoutParams = LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(10) }
        setOnClickListener { action(key) }
    }
    private fun section(parent: LinearLayout): LinearLayout = column().apply {
        setPadding(dp(20), dp(18), dp(20), dp(20)); background = shape(Color.WHITE, 22, true)
        parent.addView(this, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(16) })
    }
    private fun row(parent: LinearLayout, vararg children: View) = LinearLayout(activity).apply {
        orientation = LinearLayout.HORIZONTAL
        children.forEachIndexed { i, child -> addView(child, LinearLayout.LayoutParams(0, -2, 1f).apply { topMargin = dp(10); if (i > 0) leftMargin = dp(8) }) }
        parent.addView(this)
    }
    val root = column().apply { setBackgroundColor(paper) }
    val status = label("Not connected", 26f, Color.WHITE, true)
    private val server = label("", 14f, Color.rgb(192, 215, 211))
    private val headerHint = label("", 14f, Color.rgb(192, 215, 211))
    val name = EditText(activity).apply {
        id = R.id.device_name; hint = "Phone name"; setText(Build.MODEL)
        filters = arrayOf(InputFilter.LengthFilter(80)); isSaveEnabled = false
        setSingleLine(); textSize = 16f; setTextColor(ink)
    }
    val code = EditText(activity).apply {
        id = R.id.pairing_code; hint = "Paste pb1:… connection code"; textSize = 14f
        inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
        isSingleLine = true; isSaveEnabled = false; importantForAutofill = View.IMPORTANT_FOR_AUTOFILL_NO
        filters = arrayOf(InputFilter.LengthFilter(8192)); setTextColor(ink)
        // Prevent third-party keyboards from retaining the one-time secret.
        imeOptions = android.view.inputmethod.EditorInfo.IME_FLAG_NO_PERSONALIZED_LEARNING
    }
    private val content = column().apply { setPadding(dp(20), dp(4), dp(20), dp(24)) }
    private val setup: LinearLayout
    private val screen: LinearLayout
    private val files: LinearLayout
    val screenStatus = label("Screen control is off", 20f, ink, true).apply { id = R.id.screen_control_status; accessibilityLiveRegion = View.ACCESSIBILITY_LIVE_REGION_POLITE }
    private val screenHint = label("", 14f, muted)
    private val grant = button("Set up screen access", "accessibility", true)
    private val enable10 = button("10 min", "10", true, R.id.screen_enable_10)
    private val enable15 = button("15 min", "15", false, R.id.screen_enable_15)
    private val enable60 = button("1 hour", "60", false, R.id.screen_enable_60)
    private val manual = button("Until stopped…", "manual", false, R.id.screen_enable_manual)
    private val stop = button("Stop screen control", "screen_stop", true, R.id.screen_stop)
    private val durations = column()
    private val connectionButton = button("Pause connection", "connection")
    private val notifications = button("Allow delivery notifications", "notifications")
    private val filesStatus = label("", 14f, muted)
    private val connect = button("Connect to server", "pair", true, R.id.pair_with_code)
    private val scan = button("Scan QR code instead", "scan")
    fun pairing(inProgress: Boolean) {
        connect.isEnabled = !inProgress; scan.isEnabled = !inProgress
        name.isEnabled = !inProgress; code.isEnabled = !inProgress
        connect.text = if (inProgress) "Connecting…" else "Connect to server"
    }
    private var forget: Button? = null
    init {
        val header = column().apply { setPadding(dp(24), dp(24), dp(24), dp(24)); setBackgroundColor(ink) }
        val brand = LinearLayout(activity).apply { gravity = android.view.Gravity.CENTER_VERTICAL }
        brand.addView(ImageView(activity).apply { setImageResource(R.drawable.ic_bridge); contentDescription = null }, LinearLayout.LayoutParams(dp(32), dp(32)))
        brand.addView(label("PhoneBridge", 18f, Color.WHITE, true), LinearLayout.LayoutParams(-2, -2).apply { leftMargin = dp(10) })
        header.addView(brand)
        header.addView(status, LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(22) })
        header.addView(server); header.addView(headerHint)
        header.addView(connectionButton)
        val body = column().apply { addView(header); addView(content) }
        root.addView(ScrollView(activity).apply { isFillViewport = true; addView(body) }, LinearLayout.LayoutParams(-1, -1))
        root.setOnApplyWindowInsetsListener { view, insets ->
            val bars = insets.getInsets(WindowInsets.Type.systemBars() or WindowInsets.Type.displayCutout())
            val keyboard = insets.getInsets(WindowInsets.Type.ime())
            view.setPadding(bars.left, bars.top, bars.right, maxOf(bars.bottom, keyboard.bottom)); insets
        }
        setup = section(content).apply {
            addView(label("01  /  CONNECT YOUR SERVER", 11f, accent, true))
            addView(label("Bring Hermes to your phone", 23f, ink, true))
            addView(label("On your server, run phonebridge pair --name 'My phone' --code. Paste the code below, or scan its QR.", 14f, muted))
            addView(label("Phone name", 12f, muted, true)); addView(name)
            addView(label("Connection code", 12f, muted, true)); addView(code)
            addView(connect); addView(scan)
            addView(label("Only pair with your own server. Confirm its address before connecting. A code works once.", 12f, muted), LinearLayout.LayoutParams(-1, -2).apply { topMargin = dp(12) })
        }
        screen = section(content).apply {
            addView(label("SCREEN ACCESS", 11f, accent, true)); addView(screenStatus); addView(screenHint)
            addView(grant)
            durations.addView(label("Allow your server to see and control other apps for:", 14f, muted))
            row(durations, enable10, enable15, enable60)
            durations.addView(manual); addView(durations)
            stop.background = shape(danger, 14); addView(stop)
        }
        files = section(content).apply {
            addView(label("FILE DELIVERY", 11f, accent, true))
            addView(label("Files from your agent", 20f, ink, true)); addView(filesStatus)
            addView(button("Open Downloads", "files")); addView(notifications)
            addView(label("Documents → Download/PhoneBridge\\nImages → Pictures/PhoneBridge (Gallery)", 12f, muted))
        }
        section(content).apply {
            addView(label("NEED A HAND?", 11f, muted, true))
            addView(button("Connection & battery help", "help"))
            addView(button("Android app settings", "settings"))
            addView(button("Forget server…", "disconnect").also { forget = it; it.setTextColor(danger); it.background = shape(Color.rgb(251, 238, 237), 14) })
        }
        content.addView(label("PHONEBRIDGE  /  ${BuildConfig.VERSION_NAME}\\nA private connection to your Hermes server.", 11f, muted).apply { gravity = android.view.Gravity.CENTER; setPadding(0, dp(24), 0, 0) })
    }
    fun render(credentials: DeviceCredentials?, permissions: JSONObject, prefs: SharedPreferences, remaining: Long?) {
        val paired = credentials != null
        val enabled = paired && prefs.getBoolean("enabled", false)
        val state = prefs.getString("status", "Connecting") ?: "Connecting"
        val online = enabled && state == "Connected"
        status.text = if (!paired) "Connect your phone" else if (!enabled) "Connection paused" else if (online) "Connected to Hermes" else "Reconnecting…"
        server.text = credentials?.server?.removeSuffix("/") ?: "Your agent. Your Android. One connection."
        headerHint.text = if (!paired) "Set up once. Stay in control." else if (!enabled) "Remote access is stopped" else if (online) "●  Encrypted connection · ${Build.MODEL}" else state
        setup.visibility = if (paired) View.GONE else View.VISIBLE
        listOf(screen, files).forEach { it.visibility = if (paired) View.VISIBLE else View.GONE }
        forget?.visibility = if (paired) View.VISIBLE else View.GONE
        connectionButton.visibility = if (paired) View.VISIBLE else View.GONE
        connectionButton.text = if (enabled) "Pause connection" else "Resume connection"
        val accessibility = permissions.optBoolean("accessibility")
        val active = remaining != null
        screenStatus.text = when (remaining) {
            null -> "Screen control is off"
            ScreenControl.UNTIL_STOPPED -> "On until you stop it"
            else -> "On · %d:%02d remaining".format((remaining + 999) / 1000 / 60, (remaining + 999) / 1000 % 60)
        }
        screenHint.text = if (active) "Your server can see, tap and type in other apps. Stop access at any time." else if (!accessibility) "File delivery already works. Set up Accessibility only if you want remote screen control." else "Accessibility is ready. You decide when your server can use the screen."
        grant.visibility = if (!accessibility) View.VISIBLE else View.GONE
        durations.visibility = if (accessibility && !active) View.VISIBLE else View.GONE
        stop.visibility = if (active) View.VISIBLE else View.GONE
        listOf(enable10, enable15, enable60, manual).forEach { it.isEnabled = enabled; it.alpha = if (enabled) 1f else 0.45f }
        filesStatus.text = if (online) "Ready to receive documents and images. Tap a delivery notification to open a file." else "Delivery resumes when the connection is back. Saved files stay on your phone."
        notifications.visibility = if (permissions.optBoolean("post_notifications")) View.GONE else View.VISIBLE
    }
}
