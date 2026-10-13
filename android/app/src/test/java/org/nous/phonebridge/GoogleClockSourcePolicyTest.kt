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
