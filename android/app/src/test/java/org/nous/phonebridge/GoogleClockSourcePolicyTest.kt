package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class GoogleClockSourcePolicyTest {
    private fun accepts(version: String = GoogleClockProfile.VERSION, code: Long = GoogleClockProfile.CODE, digest: String = GoogleClockProfile.APK_SHA256, signers: Set<String> = setOf(GoogleClockProfile.SIGNER_SHA256), split: Boolean = false, sdk: Int = 31, language: String = "en") = GoogleClockProfile.accepts(version, code, digest, signers, split, sdk, language)
    private fun rejected(code: String = "UNSUPPORTED", action: () -> Unit) {
        try { action(); fail("Source policy must fail closed") } catch (e: BridgeFailure) { assertEquals(code, e.code) }
    }

    @Test fun sourcePolicyAcceptsOnlyTheExactArtifactAndSignerNotVersionFamily() {
        assertTrue(accepts())
        assertTrue(accepts(sdk = 35))
        assertFalse(accepts(version = "8.2"))
        assertFalse(accepts(version = "9.2 (993392871)", code = 76120011L))
        assertFalse(accepts(code = GoogleClockProfile.CODE + 1))
        assertFalse(accepts(digest = "0".repeat(64)))
        assertFalse(accepts(signers = emptySet()))
        assertFalse(accepts(signers = setOf("7ce83c1b71f3d572fed04c8d40c5cb10ff75e6d87d9df6fbd53f0468c2905053")))
        assertFalse(accepts(signers = setOf(GoogleClockProfile.SIGNER_SHA256, "0".repeat(64))))
        assertFalse(accepts(split = true))
        assertFalse(accepts(language = "ru"))
        for (sdk in listOf(30, 32, 34, 36)) assertFalse(accepts(sdk = sdk))
        assertTrue(SamsungClockAdapter().supported("12.5.07.13", 1250713100L).isEmpty())
        assertTrue(GoogleClockAdapter().supported(GoogleClockProfile.VERSION, GoogleClockProfile.CODE).isEmpty())
    }

    @Test fun sourcePolicyRequiresIndependentCompleteCountAndEveryItemIndex() {
        val a = GoogleClockEntry("A", "", "running", "")
        val b = GoogleClockEntry("B", "", "paused", "")
        assertEquals(0, GoogleClockCollection.count(0, 0, false))
        assertEquals(2, GoogleClockCollection.count(1, 2, false))
        assertEquals(2, GoogleClockCollection.count(2, 1, true))
        rejected { GoogleClockCollection.count(1, 0, false) }
        rejected { GoogleClockCollection.count(0, 2, false) }
        rejected { GoogleClockCollection.count(1, 2, true) }
        rejected { GoogleClockCollection.count(Int.MAX_VALUE, Int.MAX_VALUE, false) }
        GoogleClockCollection.requireComplete(listOf(a, b), 2, listOf(1, 0))
        GoogleClockCollection.requireComplete(emptyList(), 0, emptyList())
        rejected { GoogleClockCollection.requireComplete(listOf(a), 2, listOf(0)) }
        rejected { GoogleClockCollection.requireComplete(listOf(a, b), 2, listOf(0, 0)) }
        rejected { GoogleClockCollection.requireComplete(listOf(a, b), 2, listOf(0, 2)) }
        rejected { GoogleClockCollection.requireComplete(listOf(a, a.copy(state = "paused")), 2, listOf(0, 1)) }
        rejected { GoogleClockCollection.requireComplete(emptyList(), -1, emptyList()) }
        rejected { GoogleClockCollection.requireComplete(emptyList(), 9, emptyList()) }
        rejected("NOT_FOUND") { GoogleClockCollection.select(listOf(a, b), "missing", "") }
    }

    @Test fun sourcePolicyRequiresCorroboratedAlarmTimeDespitePreAttachCardDescription() {
        GoogleClockIdentity.alarm("17:31", "17:31", "CI alarm", "17:31 CI alarm", "17:31 alarm")
        GoogleClockIdentity.alarm("17:31", "17:31", "CI alarm", " CI alarm", "17:31 alarm")
        GoogleClockIdentity.alarm("08:30", "08:30", "", " Alarm", "08:30 alarm")
        rejected { GoogleClockIdentity.alarm("17:31", "17:31", "CI alarm", "19:43 CI alarm", "17:31 alarm") }
        rejected { GoogleClockIdentity.alarm("17:31", "19:43", "CI alarm", " CI alarm", "17:31 alarm") }
        rejected { GoogleClockIdentity.alarm("17:31", "17:31", "CI alarm", " CI alarm", "19:43 alarm") }
        rejected { GoogleClockIdentity.alarm("17:31", "17:31", "CI alarm", " Other", "17:31 alarm") }
        rejected { GoogleClockIdentity.alarm("", "", "CI alarm", " CI alarm", " alarm") }
        rejected { GoogleClockIdentity.alarm("25:31", "25:31", "CI alarm", " CI alarm", "25:31 alarm") }
    }

    @Test fun sourcePolicyBindsClassicAndGastownTimerControlsWithoutMixingSurfaces() {
        val classic = GoogleClockTimerSurface.fromClass("android.support.v7.widget.RecyclerView")
        val gastown = GoogleClockTimerSurface.fromClass("android.widget.GridView")
        assertEquals("play_pause", classic.action)
        assertEquals("secondary_button", classic.reset)
        assertEquals("tertiary_button", classic.deletion)
        assertEquals("play_pause_button", gastown.action)
        assertEquals("reset_button", gastown.reset)
        assertEquals("delete_button", gastown.deletion)
        assertEquals("running", classic.state("CI timer", "Pause", "Reset", "Delete CI timer"))
        assertEquals("paused", classic.state("CI timer", "Start", "Reset", "Delete CI timer"))
        assertEquals("running", gastown.state("CI timer", "Pause CI timer", "Reset CI timer", "Delete CI timer"))
        assertEquals("paused", gastown.state("CI timer", "Start CI timer", "Reset CI timer", "Delete CI timer"))
        rejected { GoogleClockTimerSurface.fromClass("android.widget.ListView") }
        rejected { classic.state("CI timer", "Pause CI timer", "Reset", "Delete CI timer") }
        rejected { gastown.state("CI timer", "Pause", "Reset CI timer", "Delete CI timer") }
        rejected { classic.state("CI timer", "Stop", "Reset", "Delete CI timer") }
        rejected { classic.state("CI timer", "Start", "", "Delete CI timer") }
        rejected { classic.state("CI timer", "Pause", "Reset", "Delete Other") }
    }

    @Test fun sourcePolicyAcceptsApi35LauncherNavigationOnlyAtTheObservedPlatformBackground() {
        val bar = GoogleClockBounds(0, 2274, 1080, 2400)
        fun accepts(sdk: Int = 35, type: Int = 3, active: Boolean = false, focused: Boolean = false, pkg: String = "com.android.launcher3", bounds: GoogleClockBounds = bar, backgrounds: List<GoogleClockBounds> = listOf(bar)) = GoogleClockWindows.system(sdk, type, active, focused, pkg, bounds, backgrounds)
        assertTrue(accepts())
        assertTrue(accepts(pkg = "com.android.systemui", backgrounds = emptyList()))
        assertFalse(accepts(sdk = 31))
        assertFalse(accepts(type = 1))
        assertFalse(accepts(type = 4))
        assertFalse(accepts(active = true))
        assertFalse(accepts(focused = true))
        assertFalse(accepts(pkg = "unknown.overlay"))
        assertFalse(accepts(backgrounds = emptyList()))
        assertFalse(accepts(bounds = bar.copy(top = 100)))
        assertFalse(accepts(bounds = bar.copy(right = 900)))
        assertFalse(accepts(bounds = GoogleClockBounds(0, 0, 0, 0), backgrounds = listOf(GoogleClockBounds(0, 0, 0, 0))))
    }

    @Test fun sourcePolicyRequiresRecreatedWindowNotBackAndShowOfRetainedSingleTask() {
        rejected { GoogleClockReadback.requireRecreated(88, 88, false) }
        rejected { GoogleClockReadback.requireRecreated(88, 90, true) }
        GoogleClockReadback.requireRecreated(88, 90, false)
        assertEquals("com.android.deskclock.DeskClock", GoogleClockProfile.ACTIVITY)
        assertEquals(android.content.Intent.FLAG_ACTIVITY_NEW_TASK or android.content.Intent.FLAG_ACTIVITY_CLEAR_TASK, GoogleClockReadback.RECREATE_FLAGS)
    }

    @Test fun sourcePolicyRequiresRepeatedFreshCompleteReadbackAndResetsOnAnyMismatch() {
        val target = GoogleClockEntry("CI timer, Start Alarm", "", "paused", "")
        val survivor = GoogleClockEntry("CI survivor timer", "", "running", "")
        val expected = listOf(target, survivor)
        val stable = GoogleClockReadback(expected, 90)
        assertFalse(stable.observe(expected, 90, 1000))
        assertFalse(stable.observe(expected, 90, 1100))
        assertFalse(stable.observe(listOf(target, survivor.copy(state = "paused")), 90, 1200))
        assertFalse(stable.observe(expected.reversed(), 90, 1300))
        assertFalse(stable.observe(expected, 90, 1499))
        assertTrue(stable.observe(expected, 90, 1500))
        rejected { stable.observe(expected, 88, 1600) }
        val incomplete = GoogleClockReadback(expected, 90)
        assertFalse(incomplete.observe(listOf(target), 90, 1000))
        assertFalse(incomplete.observe(listOf(target), 90, 5000))
        val alarm = GoogleClockEntry("CI alarm, Start Alarm", "05:31", "disabled", "every day")
        val alarmReadback = GoogleClockReadback(listOf(alarm), 91)
        assertFalse(alarmReadback.observe(listOf(alarm.copy(state = "enabled")), 91, 1000))
        assertFalse(alarmReadback.observe(listOf(alarm), 91, 1500))
        assertTrue(alarmReadback.observe(listOf(alarm), 91, 1700))
    }

    @Test fun sourcePolicyComparesCompleteSurvivorsWithoutCountdownOrPositionIdentity() {
        val a = GoogleClockEntry("Label, Start Alarm", "", "running", "")
        val b = GoogleClockEntry("Other", "", "paused", "")
        val before = listOf(a, b)
        assertEquals(b, GoogleClockCollection.expected(before, a, null).single())
        assertTrue(GoogleClockCollection.same(before, listOf(b, a)))
        assertFalse(GoogleClockCollection.same(before, listOf(a)))
        assertFalse(GoogleClockCollection.same(before, listOf(a, b.copy(state = "running"))))
        assertFalse(GoogleClockCollection.same(before, listOf(a, b.copy(label = "replacement"))))
        val alarm = GoogleClockEntry("Wake", "7:00", "enabled", "Monday")
        assertFalse(GoogleClockCollection.same(listOf(alarm), listOf(alarm.copy(schedule = "Tuesday"))))
        assertEquals("disabled", GoogleClockCollection.expected(listOf(alarm), alarm, "disabled").single().state)
    }
}
