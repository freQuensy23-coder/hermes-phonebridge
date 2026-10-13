package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class SamsungClockSourcePolicyTest {
    private fun accepts(version: String = SamsungClockProfile.VERSION, code: Long = SamsungClockProfile.CODE, apk: String = SamsungClockProfile.APK_SHA256, signers: Set<String> = setOf(SamsungClockProfile.SIGNER_SHA256), split: Boolean = false, sdk: Int = 36, language: String = "en", time24: Boolean = true, manufacturer: String = "samsung") = SamsungClockProfile.accepts(version, code, apk, signers, split, sdk, language, time24, manufacturer)
    private fun rejected(code: String = "UNSUPPORTED", action: () -> Unit) {
        try { action(); fail("Samsung source policy must fail closed") } catch (e: BridgeFailure) { assertEquals(code, e.code) }
    }

    @Test fun exactInspectedArtifactCurrentSignerAndLocaleAreRequired() {
        assertTrue(accepts())
        assertTrue(accepts(manufacturer = "Samsung"))
        assertFalse(accepts(version = "12.4.00.8", code = 1240008100L))
        assertFalse(accepts(version = "12.6.05.24"))
        assertFalse(accepts(code = SamsungClockProfile.CODE + 1))
        assertFalse(accepts(apk = "0".repeat(64)))
        assertFalse(accepts(signers = emptySet()))
        assertFalse(accepts(signers = setOf("0".repeat(64))))
        assertFalse(accepts(signers = setOf(SamsungClockProfile.SIGNER_SHA256, "0".repeat(64))))
        assertFalse(accepts(split = true))
        assertFalse(accepts(language = "ru"))
        assertFalse(accepts(time24 = false))
        assertFalse(accepts(manufacturer = "Google"))
        for (sdk in listOf(31, 34, 35, 37)) assertFalse(accepts(sdk = sdk))
        assertEquals(8, SamsungClockProfile.operations.size)
        assertFalse("Native dispatch is not a UI mutation", "clock.alarms.create" in SamsungClockProfile.operations)
    }

    @Test fun independentCollectionCountEveryIndexAndCollisionExclusion() {
        val a = SamsungClockEntry("First", "17:31", "enabled", "Monday, ")
        val b = SamsungClockEntry("Second", "19:43", "disabled", "Tuesday, ")
        assertEquals(0, SamsungClockPolicy.count(0, 0))
        assertEquals(0, SamsungClockPolicy.count(0, 1))
        assertEquals(2, SamsungClockPolicy.count(2, 1))
        SamsungClockPolicy.complete(listOf(a, b), 2, listOf(1, 0))
        SamsungClockPolicy.complete(emptyList(), 0, emptyList())
        rejected { SamsungClockPolicy.count(2, 2) }
        rejected { SamsungClockPolicy.count(Int.MAX_VALUE, Int.MAX_VALUE) }
        rejected { SamsungClockPolicy.count(1, 0) }
        rejected { SamsungClockPolicy.complete(listOf(a), 2, listOf(0)) }
        rejected { SamsungClockPolicy.complete(listOf(a, b), 2, listOf(0, 0)) }
        rejected { SamsungClockPolicy.complete(listOf(a, b), 2, listOf(0, 2)) }
        rejected { SamsungClockPolicy.complete(listOf(a, a.copy(schedule = "Tuesday, ")), 2, listOf(0, 1)) }
        rejected { SamsungClockPolicy.complete(emptyList(), 9, emptyList()) }
        val duplicateTimer = SamsungClockEntry("01:00:00", "", "running")
        rejected { SamsungClockPolicy.complete(listOf(duplicateTimer, duplicateTimer.copy(state = "paused")), 2, listOf(0, 1)) }
    }

    @Test fun exactCurrentDisplaySelectionRequiresExpectedTimerState() {
        val a = SamsungClockEntry("Label, Delete Alarm", "", "running")
        val b = SamsungClockEntry("Label, Delete Alarm decoy", "", "paused")
        assertEquals(a, SamsungClockPolicy.select(listOf(a, b), a.label, "", "running"))
        rejected { SamsungClockPolicy.select(listOf(a, b), a.label, "", "paused") }
        rejected("NOT_FOUND") { SamsungClockPolicy.select(listOf(a, b), "Label", "", "running") }
        rejected { SamsungClockPolicy.select(listOf(a, a), a.label, "", "running") }
        val alarm = SamsungClockEntry("Arbitrary, Delete Alarm", "17:31", "enabled")
        assertEquals("17:31 Arbitrary, Delete Alarm", alarm.menuHeader)
        rejected("NOT_FOUND") { SamsungClockPolicy.select(listOf(alarm), alarm.label, "5:31", null) }
    }

    @Test fun survivorReadbackUsesMultisetsScheduleAndStateWithoutCountdownOrPosition() {
        val a = SamsungClockEntry("Target", "", "running")
        val b = SamsungClockEntry("Decoy", "", "paused")
        val before = listOf(a, b)
        assertTrue(SamsungClockPolicy.same(before, listOf(b, a)))
        assertFalse(SamsungClockPolicy.same(before, listOf(a, a)))
        assertFalse(SamsungClockPolicy.same(before, listOf(a, b.copy(state = "running"))))
        assertEquals(listOf(b), SamsungClockPolicy.expected(before, a, null))
        assertEquals(listOf(a.copy(state = "paused"), b), SamsungClockPolicy.expected(before, a, "paused"))
        val alarm = SamsungClockEntry("Wake", "17:31", "enabled", "Monday, ")
        assertFalse(SamsungClockPolicy.same(listOf(alarm), listOf(alarm.copy(schedule = "Tuesday, "))))
        assertFalse(SamsungClockPolicy.same(before, listOf(a.copy(label = "replacement"), b)))
    }

    @Test fun sourcePauseResumeAndPositiveRemainingMarginExcludeExpiryRestartAndGuessing() {
        assertEquals("running", SamsungClockPolicy.timerState("Pause"))
        assertEquals("paused", SamsungClockPolicy.timerState("Resume"))
        for (description in listOf("Restart", "Stop", "Start", "pause", "Pause Target", "Пауза", "")) rejected { SamsungClockPolicy.timerState(description) }
        assertEquals(7200L, SamsungClockPolicy.remaining("02:00:00"))
        assertEquals(10L, SamsungClockPolicy.remaining("00:00:10"))
        assertEquals(86400L, SamsungClockPolicy.remaining("24:00:00"))
        for (time in listOf("00:00:00", "00:00:09", "-00:00:01", "00:60:00", "2:00:00", "99:00:00", "Remaining 02:00:00")) rejected { SamsungClockPolicy.remaining(time) }
    }

    @Test fun sourceDescriptionsCorroborateWholeLabelTimeAndScheduleWithoutSplittingLabels() {
        val label = "Label, Delete, Double tap to edit."
        val spokenTime = "17 hours 31 minutes"
        val switchDescription = "$label, $spokenTime, Monday, Tuesday"
        val itemDescription = "$switchDescription, , Double tap to edit."
        assertEquals("Monday, Tuesday, ", SamsungClockPolicy.alarmSchedule(label, spokenTime, switchDescription, itemDescription))
        rejected { SamsungClockPolicy.alarmSchedule("Label", spokenTime, switchDescription, itemDescription) }
        rejected { SamsungClockPolicy.alarmSchedule(label, "19 hours 31 minutes", switchDescription, itemDescription) }
        rejected { SamsungClockPolicy.alarmSchedule(label, spokenTime, switchDescription + " altered", itemDescription) }
        rejected { SamsungClockPolicy.alarmSchedule(label, spokenTime, switchDescription, "$switchDescription, Double tap to edit.") }
        rejected { SamsungClockPolicy.alarmSchedule(label, spokenTime, switchDescription, itemDescription.replace("Double tap to edit.", "Edit")) }
        val header = SamsungClockEntry(label, "17:31", "enabled").menuHeader
        SamsungClockPolicy.popupTexts(listOf(header, "Delete"), header)
        rejected { SamsungClockPolicy.popupTexts(listOf("17:31 another alarm", "Delete"), header) }
        rejected { SamsungClockPolicy.popupTexts(listOf(header, "Move", "Delete"), header) }
        rejected { SamsungClockPolicy.popupTexts(listOf(header, "Delete", "Delete"), header) }
        rejected { SamsungClockPolicy.popupTexts(listOf("Delete"), header) }
    }

    @Test fun truncatedDirectionalAndUnboundedIdentitiesAreRejected() {
        assertEquals("Target, Delete Alarm", SamsungClockPolicy.text("Target, Delete Alarm"))
        for (text in listOf("", "clipped…", "clipped...", "A\u0000B", "A\nB", "A\u202eB", "A\u2066B", "A".repeat(257), "\ud800")) rejected { SamsungClockPolicy.text(text) }
    }
}
