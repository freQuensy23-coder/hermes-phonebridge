package org.nous.phonebridge

import android.content.Intent
import android.provider.AlarmClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class ClockIntentInstrumentationTest {
    private fun dismiss(args: JSONObject) = Intent(AlarmClock.ACTION_DISMISS_ALARM).also { NativeClocks.dismissExtras(it, Arguments(args)) }
    private fun rejected(args: JSONObject) {
        try { dismiss(args); fail("Invalid dismiss selector must be rejected") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
    }

    @Test fun hourOnlyPreservesEveryValidated24HourSelectionWithTypedAmPm() {
        for (hour in 0..23) {
            val intent = dismiss(JSONObject().put("hour", hour))
            assertEquals(AlarmClock.ALARM_SEARCH_MODE_TIME, intent.getStringExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE))
            assertEquals(if (hour > 12) hour - 12 else hour, intent.getIntExtra(AlarmClock.EXTRA_HOUR, -1))
            assertEquals(hour >= 12, intent.getBooleanExtra(AlarmClock.EXTRA_IS_PM, hour < 12))
            assertTrue(intent.extras!!.get(AlarmClock.EXTRA_HOUR) is Int)
            assertTrue(intent.extras!!.get(AlarmClock.EXTRA_IS_PM) is Boolean)
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_MINUTES))
            val encoded = intent.getIntExtra(AlarmClock.EXTRA_HOUR, -1)
            val decoded = if (intent.getBooleanExtra(AlarmClock.EXTRA_IS_PM, false) && encoded < 12) encoded + 12 else encoded
            assertEquals(hour, decoded)
        }
    }

    @Test fun minuteOnlyEmitsNoInventedHourOrAmPm() {
        for (minute in listOf(0, 17, 59)) {
            val intent = dismiss(JSONObject().put("minute", minute))
            assertEquals(AlarmClock.ALARM_SEARCH_MODE_TIME, intent.getStringExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE))
            assertEquals(minute, intent.getIntExtra(AlarmClock.EXTRA_MINUTES, -1))
            assertTrue(intent.extras!!.get(AlarmClock.EXTRA_MINUTES) is Int)
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_HOUR))
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_IS_PM))
        }
    }

    @Test fun midnightNoonAndPmCompleteSelectorsRetainBothDimensions() {
        for ((hour, encoded, pm) in listOf(Triple(0, 0, false), Triple(12, 12, true), Triple(23, 11, true))) {
            val intent = dismiss(JSONObject().put("hour", hour).put("minute", 45))
            assertEquals(encoded, intent.getIntExtra(AlarmClock.EXTRA_HOUR, -1))
            assertEquals(pm, intent.getBooleanExtra(AlarmClock.EXTRA_IS_PM, !pm))
            assertEquals(45, intent.getIntExtra(AlarmClock.EXTRA_MINUTES, -1))
            assertTrue(intent.extras!!.get(AlarmClock.EXTRA_MINUTES) is Int)
        }
    }

    @Test fun labelAndDefaultAllNeverAcquireTimeExtras() {
        val label = dismiss(JSONObject().put("label", "Wake up"))
        assertEquals(AlarmClock.ALARM_SEARCH_MODE_LABEL, label.getStringExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE))
        assertEquals("Wake up", label.getStringExtra(AlarmClock.EXTRA_MESSAGE))
        val all = dismiss(JSONObject())
        assertEquals(AlarmClock.ALARM_SEARCH_MODE_ALL, all.getStringExtra(AlarmClock.EXTRA_ALARM_SEARCH_MODE))
        for (intent in listOf(label, all)) {
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_HOUR))
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_MINUTES))
            assertFalse(intent.hasExtra(AlarmClock.EXTRA_IS_PM))
        }
        rejected(JSONObject().put("label", "Wake up").put("hour", 0))
        rejected(JSONObject().put("label", "Wake up").put("minute", 0))
        rejected(JSONObject().put("label", "Wake up").put("hour", 12).put("minute", 0))
    }

    @Test fun partialSelectorsStillRejectNullWrongTypesRangesAndUnknownExtras() {
        for (value in listOf(JSONObject.NULL, true, "7", 7.0, -1, 24)) rejected(JSONObject().put("hour", value))
        for (value in listOf(JSONObject.NULL, false, "15", 15.0, -1, 60)) rejected(JSONObject().put("minute", value))
        rejected(JSONObject().put("hour", 12).put("is_pm", true))
    }
}
