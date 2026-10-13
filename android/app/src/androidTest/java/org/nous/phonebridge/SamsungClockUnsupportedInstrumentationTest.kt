package org.nous.phonebridge

import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith

@RunWith(AndroidJUnit4::class)
class SamsungClockUnsupportedInstrumentationTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext

    @Test fun unknownInstalledSamsungIsReportedUnsupportedNotPositiveProof() {
        val evidence = SamsungClockEvidence(context, "unsupported-inventory")
        val inventory = evidence.inventory()
        val eligible = SamsungClockProfile.installed(context)
        inventory.put("runtime_positive_proof", false).put("status", if (eligible) "SOURCE_PROFILE_ELIGIBLE_RUNTIME_UNVERIFIED" else "UNSUPPORTED")
        android.util.Log.i("SamsungClockUnsupported", inventory.toString())
        if (!eligible) {
            try {
                SamsungClockProfile.requireInstalled(context)
                fail("Unknown installed Samsung profile must fail closed")
            } catch (e: BridgeFailure) {
                assertEquals("UNSUPPORTED", e.code)
            }
        }
        assertTrue(SamsungClockAdapter().supported("12.4.00.8", 1240008100L).isEmpty())
        assertTrue(SamsungClockAdapter().supported("12.6.05.24", 1260524100L).isEmpty())
        assertTrue(SamsungClockAdapter().supported("unknown", 0).isEmpty())
        assertTrue(SamsungClockAdapter().reason("unknown", 0).contains("UNSUPPORTED"))
        assertTrue(SamsungClockAdapter().reason(SamsungClockProfile.VERSION, SamsungClockProfile.CODE).contains("unverified"))
        evidence.capture("unsupported-inventory-no-positive-proof")
        evidence.flush()
    }

    @Test fun wrongExplicitPackageAndVersionCannotAcquireSamsungProfileSupport() {
        val adapter = ClockAdapters(context).forPackage(SamsungClockProfile.PACKAGE)
        assertEquals("samsung_clock_12_5_07_13_source_candidate", adapter.name)
        assertTrue(adapter.supported("12.5.07.13", SamsungClockProfile.CODE + 1).isEmpty())
        assertTrue(adapter.supported("12.5.07.13 arbitrary", SamsungClockProfile.CODE).isEmpty())
        assertFalse(SamsungClockProfile.accepts(SamsungClockProfile.VERSION, SamsungClockProfile.CODE, SamsungClockProfile.APK_SHA256, setOf(SamsungClockProfile.SIGNER_SHA256), false, 35, "en", true, "Google"))
        android.util.Log.i("SamsungClockUnsupported", JSONObject().put("status", "NEGATIVE_POLICY_ONLY").put("runtime_positive_proof", false).toString())
    }
}
