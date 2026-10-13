package org.nous.phonebridge

import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import java.util.Calendar

class ProtocolTest {
    private fun qr(server: String = "https://phone.example", expiry: Long = 1300, version: Int = 1) = JSONObject().put("v", version).put("server", server).put("token", "A".repeat(43)).put("expires_at", expiry).toString()
    private fun rejected(block: () -> Unit) {
        try { block(); fail("Expected rejection") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
    }
    @Test fun pairingAcceptsHttpsAndReturnsExactOrigin() {
        val parsed = PairingQr.parse(qr(), false, 1000)
        assertEquals("phone.example", parsed.server.host)
        assertEquals(1300L, parsed.expiresAt)
        assertEquals("A".repeat(43), parsed.token)
    }
    @Test fun pairingRejectsExpiryAndUnknownVersions() {
        rejected { PairingQr.parse(qr(expiry = 1000), false, 1000) }
        rejected { PairingQr.parse(qr(expiry = 100000), false, 1000) }
        rejected { PairingQr.parse(qr(version = 2), false, 1000) }
    }
    @Test fun pairingRejectsCredentialsFragmentsPathsAndQueryTokens() {
        for (url in listOf("https://u:p@phone.example", "https://phone.example/#token", "https://phone.example/path", "https://phone.example?token=secret", "file:///etc/passwd", "http://phone.example")) rejected { PairingQr.parse(qr(url), false, 1000) }
    }
    @Test fun debugHttpIsLimitedToEmulatorHost() {
        assertEquals("10.0.2.2", PairingQr.parse(qr("http://10.0.2.2:18940"), true, 1000).server.host)
        rejected { PairingQr.parse(qr("http://10.0.2.2:18940"), false, 1000) }
        rejected { PairingQr.parse(qr("http://localhost:18940"), true, 1000) }
    }
    @Test fun exactTokenIsNotNormalized() {
        val value = JSONObject(qr()).put("token", " A".repeat(30)).toString()
        rejected { PairingQr.parse(value, false, 1000) }
    }
    @Test fun typedArgumentsRejectStringsFractionsAndUnknownKeys() {
        for (value in listOf<Any>("1", true, 1.0, 1.5, 0, 61)) rejected { Arguments(JSONObject().put("minute", value)).int("minute", 1, 60) }
        assertEquals(60, Arguments(JSONObject().put("minute", 60)).int("minute", 1, 60))
        rejected { Arguments(JSONObject().put("shell", "id")).only("minute") }
        rejected { Arguments(JSONObject().put("enabled", "true")).bool("enabled") }
    }
    @Test fun mondaySundayTranslationIsTypedAndUnique() {
        assertArrayEquals(intArrayOf(Calendar.MONDAY, Calendar.SUNDAY, Calendar.WEDNESDAY), calendarDays(JSONArray(listOf(1, 7, 3))))
        rejected { calendarDays(JSONArray(listOf(1, 1))) }
        rejected { calendarDays(JSONArray(listOf(0))) }
        rejected { calendarDays(JSONArray(listOf("1"))) }
        rejected { calendarDays(JSONArray()) }
    }
    @Test fun canonicalObjectOrderingPreservesDedupIdentity() {
        val first = JSONObject().put("b", JSONObject().put("z", 1).put("a", 2)).put("a", JSONArray(listOf(3, 4)))
        val second = JSONObject().put("a", JSONArray(listOf(3, 4))).put("b", JSONObject().put("a", 2).put("z", 1))
        assertEquals(DurableStore.canonical(first), DurableStore.canonical(second))
    }
    @Test fun payloadBoundsMatchServerNestingItemsAndStrings() {
        PayloadBounds.validate(JSONObject().put("ok", true))
        rejected { PayloadBounds.validate(JSONObject().put("text", "x".repeat(16385))) }
        rejected { PayloadBounds.validate(JSONObject().put("items", JSONArray((0..512).toList()))) }
        var nested = JSONObject()
        repeat(14) { nested = JSONObject().put("nested", nested) }
        rejected { PayloadBounds.validate(nested) }
    }
    @Test fun terminalResultsHaveExclusiveSuccessErrorShape() {
        val good = Results.ok("test", JSONObject().put("outcome", "dispatched").put("verified", false))
        assertTrue(good.getBoolean("ok"))
        assertTrue(good.isNull("error"))
        val error = Results.error("test", "AMBIGUOUS_STATE", "No replay")
        assertFalse(error.getBoolean("ok"))
        assertTrue(error.isNull("data"))
        assertEquals("AMBIGUOUS_STATE", error.getJSONObject("error").getString("code"))
    }
}
