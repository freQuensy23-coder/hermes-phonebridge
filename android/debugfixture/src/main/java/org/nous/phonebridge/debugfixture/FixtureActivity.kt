package org.nous.phonebridge.debugfixture

import android.app.Activity
import android.graphics.Color
import android.os.Bundle
import android.text.InputType
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView

class FixtureActivity : Activity() {
    private var counter = 0
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.setSoftInputMode(WindowManager.LayoutParams.SOFT_INPUT_STATE_ALWAYS_HIDDEN)
        val mode = intent.getStringExtra("screen") ?: "ordinary"
        if (mode == "secure") window.addFlags(WindowManager.LayoutParams.FLAG_SECURE)
        val layout = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(12), dp(12), dp(12), dp(12))
            setBackgroundColor(Color.rgb(226, 240, 255))
            isFocusableInTouchMode = true
        }
        layout.addView(TextView(this).apply {
            id = R.id.fixture_title
            text = "Synthetic debug fixture: $mode"
            textSize = 21f
            setTextColor(Color.rgb(12, 30, 100))
        }, LinearLayout.LayoutParams(-1, dp(48)))
        val count = TextView(this).apply {
            id = R.id.counter
            text = "Counter: 0"
            textSize = 24f
            setTextColor(Color.BLACK)
        }
        layout.addView(count, LinearLayout.LayoutParams(-1, dp(48)))
        layout.addView(Button(this).apply {
            id = R.id.increment
            text = "Increment exactly once"
            setOnClickListener { counter++; count.text = "Counter: $counter" }
        }, LinearLayout.LayoutParams(-1, dp(56)))
        layout.addView(EditText(this).apply {
            id = R.id.editor
            setSingleLine(true)
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_NO_SUGGESTIONS
            setText("Synthetic initial text")
            isSaveEnabled = false
        }, LinearLayout.LayoutParams(-1, dp(56)))
        if (mode == "password") {
            layout.addView(EditText(this).apply {
                id = R.id.password
                inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_VARIATION_PASSWORD
                setText("Synthetic password only")
                isSaveEnabled = false
            }, LinearLayout.LayoutParams(-1, dp(56)))
        }
        val rows = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        repeat(20) { index ->
            rows.addView(TextView(this).apply {
                text = "Synthetic scroll row ${index.toString().padStart(2, '0')}"
                textSize = 18f
                setTextColor(Color.rgb(20, 30, 45))
                setBackgroundColor(if (index % 2 == 0) Color.rgb(255, 220, 120) else Color.rgb(140, 215, 180))
                setPadding(dp(12), dp(10), dp(12), dp(10))
            }, LinearLayout.LayoutParams(-1, dp(60)))
        }
        layout.addView(ScrollView(this).apply {
            id = R.id.scroll
            addView(rows)
        }, LinearLayout.LayoutParams(-1, 0, 1f))
        setContentView(layout)
        layout.requestFocus()
    }
    private fun dp(value: Int) = (value * resources.displayMetrics.density).toInt()
}
