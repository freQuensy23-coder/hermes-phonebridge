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

    @Test fun sourcePolicyAcceptsOnlyCompleteAlarmDetailWithoutPromotionOrCollectionControls() {
        val detail = listOf("clock", "alarm_label", "repeat_days", "save_button", "delete_button")
        GoogleClockAlarmDetail.requireSurface(detail)
        GoogleClockAlarmDetail.requireSurface(detail + listOf("clock_container", "label_setting", "vibrate"))
        for (resource in detail) {
            rejected { GoogleClockAlarmDetail.requireSurface(detail - resource) }
            rejected { GoogleClockAlarmDetail.requireSurface(detail + resource) }
        }
        for (resource in listOf("body", "dismiss_button", "alarm_label_edit", "loading_indicator", "pause_alarm_cancel_button", "alarm_recycler_view", "timer_view", "onoff", "play_pause", "play_pause_button", "timer_label", "reset_button", "workflow", "watch_alarm_sound", "unknown")) {
            rejected { GoogleClockAlarmDetail.requireSurface(detail + resource) }
        }
        rejected { GoogleClockAlarmDetail.requireSurface(listOf("header", "body", "dismiss_button")) }
        rejected { GoogleClockAlarmDetail.requireSurface(listOf("alarm_recycler_view", "onoff", "digital_clock")) }
        rejected { GoogleClockAlarmDetail.requireSurface(listOf("timer_view", "timer_label", "delete_button")) }
    }

    private val modalTarget = GoogleClockEntry("CI alarm, Start Alarm", "05:31", "enabled", "every day")
    private val modalSurvivor = GoogleClockEntry("CI survivor", "05:43", "enabled", "Monday, Tuesday")
    private val modalBaseline get() = listOf(modalTarget, modalSurvivor)
    private val modalDays get() = GoogleClockAlarmDetail.weekdays.map { it to true }
    private fun modalDetail(transaction: GoogleClockAlarmDelete, window: Int = 91, label: String = modalTarget.label, time: String = modalTarget.time, description: String = time, days: List<Pair<String, Boolean>> = modalDays, now: Long = 2000) = transaction.detail(window, label, time, description, days, now)
    private fun modalTransaction(phase: GoogleClockAlarmDelete.Phase = GoogleClockAlarmDelete.Phase.BASELINE): GoogleClockAlarmDelete {
        val transaction = GoogleClockAlarmDelete(modalBaseline, 90, modalTarget, 1000)
        if (phase == GoogleClockAlarmDelete.Phase.BASELINE) return transaction
        assertFalse(transaction.baseline(modalBaseline, 90, 1000))
        transaction.select(modalBaseline.reversed(), 90, 1200)
        if (phase == GoogleClockAlarmDelete.Phase.SELECTED) return transaction
        assertFalse(modalDetail(transaction, now = 1300))
        assertTrue(modalDetail(transaction, now = 2000))
        if (phase == GoogleClockAlarmDelete.Phase.DETAIL) return transaction
        transaction.dispatch(91, 2000)
        if (phase == GoogleClockAlarmDelete.Phase.DISPATCHED) return transaction
        assertFalse(transaction.returned(listOf(modalSurvivor), 90, false, 2100))
        assertTrue(transaction.returned(listOf(modalSurvivor), 90, false, 2300))
        if (phase == GoogleClockAlarmDelete.Phase.RETURNED) return transaction
        transaction.recreate(2300)
        if (phase == GoogleClockAlarmDelete.Phase.RECREATED) return transaction
        assertFalse(transaction.complete(listOf(modalSurvivor), 92, false, false, 2400))
        assertTrue(transaction.complete(listOf(modalSurvivor), 92, false, false, 2600))
        return transaction
    }

    @Test fun modalSafeGraphAcceptsOnlyOneFreshFocusedGoogleApplicationWithoutHiddenHost() {
        val detail = GoogleClockApplication(91, GoogleClockProfile.PACKAGE, true, true)
        GoogleClockModalGraph.requireSingle(listOf(detail), 91, 1, 1, true)
        for (applications in listOf(emptyList(), listOf(detail, detail), listOf(detail, GoogleClockApplication(90, GoogleClockProfile.PACKAGE, false, false)), listOf(detail, GoogleClockApplication(93, GoogleClockProfile.PACKAGE, false, false)), listOf(detail.copy(pkg = "foreign.app")), listOf(detail.copy(active = false)), listOf(detail.copy(focused = false)), listOf(detail.copy(window = 90)))) {
            rejected { GoogleClockModalGraph.requireSingle(applications, 91, 1, 1, true) }
        }
        rejected { GoogleClockModalGraph.requireSingle(listOf(detail), 91, 2, 1, true) }
        rejected { GoogleClockModalGraph.requireSingle(listOf(detail), 91, 1, 2, true) }
        rejected { GoogleClockModalGraph.requireSingle(listOf(detail), 91, 0, 0, true) }
        rejected { GoogleClockModalGraph.requireSingle(listOf(detail), 91, 1, 1, false) }
        rejected { GoogleClockModalGraph.requireSingle(listOf(detail.copy(visible = false)), 91, 1, 1, true) }
    }

    @Test fun modalSafeProtocolRequiresEveryFreshPhaseBeforeSuccessAndOneDispatch() {
        for (phase in listOf(GoogleClockAlarmDelete.Phase.BASELINE, GoogleClockAlarmDelete.Phase.SELECTED, GoogleClockAlarmDelete.Phase.DETAIL, GoogleClockAlarmDelete.Phase.DISPATCHED, GoogleClockAlarmDelete.Phase.RETURNED, GoogleClockAlarmDelete.Phase.RECREATED, GoogleClockAlarmDelete.Phase.COMPLETE)) assertEquals(phase, modalTransaction(phase).phase)
        val complete = modalTransaction(GoogleClockAlarmDelete.Phase.COMPLETE)
        assertEquals(listOf(modalSurvivor), complete.expected)
        rejected("AMBIGUOUS_STATE") { complete.dispatch(91, 2700) }
        assertEquals(GoogleClockAlarmDelete.Phase.ABORTED, complete.phase)
        rejected("AMBIGUOUS_STATE") { complete.complete(listOf(modalSurvivor), 92, false, false, 2800) }
        rejected { modalTransaction().dispatch(91, 2000) }
        rejected { modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED).dispatch(91, 2000) }
        rejected { modalTransaction().returned(listOf(modalSurvivor), 90, false, 2000) }
        rejected { modalTransaction().recreate(2000) }
        rejected { modalTransaction().complete(listOf(modalSurvivor), 92, false, false, 2000) }
        rejected { modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL).complete(listOf(modalSurvivor), 92, false, false, 2100) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).dispatch(91, 2100) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).recreate(2100) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RETURNED).complete(listOf(modalSurvivor), 92, false, false, 2400) }
        rejected("AMBIGUOUS_STATE") { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.RETURNED), now = 2400) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).returned(listOf(modalSurvivor), 90, false, 2400) }
    }

    @Test fun modalSafeBaselineIsCompleteUniqueStableFreshAndBoundedBeforeCardClick() {
        rejected { GoogleClockAlarmDelete(listOf(modalTarget, modalTarget), 90, modalTarget, 1000) }
        rejected { GoogleClockAlarmDelete(modalBaseline, 90, modalTarget.copy(schedule = ""), 1000) }
        rejected { GoogleClockAlarmDelete(listOf(modalSurvivor), 90, modalTarget, 1000) }
        rejected { modalTransaction().select(modalBaseline, 90, 1000) }
        val unstable = modalTransaction()
        assertFalse(unstable.baseline(modalBaseline, 90, 1000))
        rejected { unstable.select(modalBaseline, 90, 1199) }
        for (values in listOf(listOf(modalTarget), modalBaseline + modalSurvivor, listOf(modalTarget, modalSurvivor.copy(state = "disabled")), listOf(modalTarget, modalSurvivor.copy(schedule = "Wednesday")))) rejected { modalTransaction().baseline(values, 90, 1200) }
        rejected { modalTransaction().baseline(modalBaseline, 91, 1200) }
        rejected { modalTransaction().baseline(modalBaseline, 90, 11000) }
        rejected { modalTransaction().baseline(modalBaseline, 90, 999) }
        rejected { modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED).select(modalBaseline, 90, 1300) }
    }

    @Test fun modalSafeDetailRejectsChangedLabelTimeRecurrenceWindowAndStaleOrLateProof() {
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), window = 90) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), label = "Other") }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), time = "05:43") }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), description = "05:43") }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), days = modalDays.dropLast(1)) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), days = modalDays.dropLast(1) + modalDays.first()) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), days = modalDays.map { it.first to false }) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), days = modalDays.dropLast(1) + ("unknown" to true)) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL), window = 93, now = 2100) }
        rejected { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED), now = 3200) }
        rejected { modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL).dispatch(91, 2101) }
        rejected { modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL).dispatch(93, 2000) }
        val notStable = modalTransaction(GoogleClockAlarmDelete.Phase.SELECTED)
        assertFalse(modalDetail(notStable, now = 1300))
        assertFalse(modalDetail(notStable, now = 1999))
        rejected { notStable.dispatch(91, 1999) }
        val ready = modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL)
        assertTrue(modalDetail(ready, now = 2200))
        ready.dispatch(91, 2200)
        rejected("AMBIGUOUS_STATE") { ready.dispatch(91, 2201) }
    }

    @Test fun modalSafeFreshTraversalStillRequiresProofAndDispatchInsideTheOriginalSelectionBound() {
        val lastPermitted = modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL)
        assertTrue(modalDetail(lastPermitted, now = 3199))
        lastPermitted.dispatch(91, 3199)
        val expiredSelection = modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL)
        assertTrue(modalDetail(expiredSelection, now = 3199))
        rejected { expiredSelection.dispatch(91, 3200) }
        val expiredProof = modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL)
        rejected { modalDetail(expiredProof, now = 3200) }
        val staleFinalRead = modalTransaction(GoogleClockAlarmDelete.Phase.DETAIL)
        rejected { staleFinalRead.dispatch(91, 2101) }
    }

    @Test fun modalSafeRecurrenceBindsSevenIndependentNamedCheckboxesToTheSelectedSchedule() {
        GoogleClockAlarmDetail.requireIdentity(modalTarget, modalTarget.label, modalTarget.time, modalTarget.time, modalDays.reversed())
        val once = modalTarget.copy(schedule = "")
        GoogleClockAlarmDetail.requireIdentity(once, once.label, once.time, once.time, modalDays.map { it.first to false })
        val selectedDays = modalDays.map { it.first to (it.first in setOf("Monday", "Tuesday")) }
        GoogleClockAlarmDetail.requireIdentity(modalSurvivor, modalSurvivor.label, modalSurvivor.time, modalSurvivor.time, selectedDays)
        rejected { GoogleClockAlarmDetail.requireIdentity(modalTarget.copy(schedule = "Monday, Monday"), modalTarget.label, modalTarget.time, modalTarget.time, selectedDays) }
        rejected { GoogleClockAlarmDetail.requireIdentity(modalTarget.copy(schedule = "unknown"), modalTarget.label, modalTarget.time, modalTarget.time, selectedDays) }
        rejected { GoogleClockAlarmDetail.requireIdentity(modalTarget.copy(schedule = "Monday, Tuesday, "), modalTarget.label, modalTarget.time, modalTarget.time, selectedDays) }
        rejected { GoogleClockAlarmDetail.requireIdentity(modalSurvivor, modalSurvivor.label, modalSurvivor.time, modalSurvivor.time, modalDays) }
    }

    @Test fun modalSafeReturnedStabilityCannotBorrowPostDispatchModalTime() {
        for (observations in listOf(emptyList<Long>(), listOf(2100L), listOf(2100L, 2400L))) {
            val transaction = modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED)
            for (now in observations) assertFalse(modalDetail(transaction, now = now))
            assertFalse(transaction.returned(listOf(modalSurvivor), 90, false, 2600))
            assertFalse(transaction.returned(listOf(modalSurvivor), 90, false, 2799))
            assertTrue(transaction.returned(listOf(modalSurvivor), 90, false, 2800))
            transaction.recreate(2800)
            assertEquals(GoogleClockAlarmDelete.Phase.RECREATED, transaction.phase)
        }
        val premature = modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED)
        assertFalse(modalDetail(premature, now = 2100))
        assertFalse(modalDetail(premature, now = 2400))
        assertFalse(premature.returned(listOf(modalSurvivor), 90, false, 2600))
        rejected("AMBIGUOUS_STATE") { premature.recreate(2799) }
    }

    @Test fun modalSafeReturnedCollectionAndRecreationCannotSkipStableSurvivorOrWindowProof() {
        val unexpected = listOf(modalBaseline, emptyList(), listOf(modalSurvivor.copy(state = "disabled")), listOf(modalSurvivor.copy(schedule = "Wednesday")), listOf(modalSurvivor, modalSurvivor), listOf(modalSurvivor, modalTarget.copy(label = "concurrent")))
        for (values in unexpected) {
            rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).returned(values, 90, false, 2100) }
            rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).complete(values, 92, false, false, 2400) }
        }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).returned(listOf(modalSurvivor), 93, false, 2100) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).returned(listOf(modalSurvivor), 90, true, 2100) }
        val unstable = modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED)
        assertFalse(unstable.returned(listOf(modalSurvivor), 90, false, 2100))
        rejected("AMBIGUOUS_STATE") { unstable.recreate(2299) }
        for (window in listOf(90, 91)) rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).complete(listOf(modalSurvivor), window, false, false, 2400) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).complete(listOf(modalSurvivor), 92, true, false, 2400) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).complete(listOf(modalSurvivor), 92, false, true, 2400) }
        val changed = modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED)
        assertFalse(changed.complete(listOf(modalSurvivor), 92, false, false, 2400))
        rejected("AMBIGUOUS_STATE") { changed.complete(listOf(modalSurvivor), 93, false, false, 2600) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED).returned(listOf(modalSurvivor), 90, false, 12000) }
        rejected("AMBIGUOUS_STATE") { modalTransaction(GoogleClockAlarmDelete.Phase.RECREATED).complete(listOf(modalSurvivor), 92, false, false, 12300) }
        rejected("AMBIGUOUS_STATE") { modalDetail(modalTransaction(GoogleClockAlarmDelete.Phase.DISPATCHED), label = "Other", now = 2100) }
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
