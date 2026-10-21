package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test
import java.util.UUID

class DebugControlTest {
    private var elapsed = 1000L
    private fun policy() = DebugControl { elapsed }
    private fun denied(block: () -> Unit) {
        try { block(); fail("Expected consent rejection") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
    }
    @Test fun startsOffAndNewInstancesNeverRestoreConsent() {
        val first = policy()
        assertFalse(first.status().enabled)
        assertNull(first.status().debugSession)
        assertEquals(0L, first.status().remainingSeconds)
        first.enableLocal(null)
        assertFalse(policy().status().enabled)
    }
    @Test fun everyLocalEnableGetsFreshUuidEvenWhileEnabled() {
        val control = policy()
        val first = control.enableLocal(10)
        val second = control.enableLocal(10)
        assertEquals(first, UUID.fromString(first).toString())
        assertNotEquals(first, second)
        denied { control.withSession(first) {} }
        control.withSession(second) {}
    }
    @Test fun deadlineRejectsAtExactBoundaryWithoutTimer() {
        for (minutes in listOf(10, 15, 60)) {
            elapsed = 1000L
            val control = policy()
            val session = control.enableLocal(minutes)
            elapsed += minutes * 60000L - 1
            assertEquals(1L, control.status().remainingSeconds)
            control.withSession(session) {}
            elapsed++
            denied { control.withSession(session) {} }
            assertFalse(control.status().enabled)
            assertEquals(0L, control.status().remainingSeconds)
        }
    }
    @Test fun onlyInjectedMonotonicTimeControlsDeadline() {
        class Clock {
            var monotonic = 1000L
            var wall = 0L
        }
        val clock = Clock()
        val control = DebugControl { clock.monotonic }
        val session = control.enableLocal(10)
        for (wallTime in listOf(Long.MIN_VALUE, Long.MAX_VALUE, 0L)) {
            clock.wall = wallTime
            control.withSession(session) {}
            assertEquals(600L, control.status().remainingSeconds)
        }
        clock.monotonic += 600000L
        denied { control.withSession(session) {} }
    }
    @Test fun indefiniteLastsUntilRevokeNotUntilAnotherGeneration() {
        val control = policy()
        val session = control.enableLocal(null)
        elapsed = Long.MAX_VALUE / 2
        assertNull(control.status().remainingSeconds)
        control.withSession(session) {}
        control.revoke()
        denied { control.withSession(session) {} }
        control.enableLocal(null)
        denied { control.withSession(session) {} }
    }
    @Test fun missingMismatchedRevokedAndReenabledSessionsCannotReadCache() {
        val control = policy()
        val session = control.enableLocal(15)
        val cache = mapOf("command-1" to "screen-data")
        val reads = mutableListOf<String>()
        fun read(expected: String?) = CommandSinks(control, expected).prepared({ "command-1" }) { id ->
            reads.add(id)
            cache[id]
        }
        denied { read(null) }
        denied { read("other") }
        control.revoke()
        denied { read(session) }
        control.enableLocal(15)
        denied { read(session) }
        assertTrue(reads.isEmpty())
    }
    @Test fun lateAsyncCallbackCannotDiscloseAfterRevokeOrReenable() {
        val control = policy()
        val session = control.enableLocal(10)
        val flight = DebugFlight(control, session)
        var disclosures = 0
        flight.effect { }
        control.revoke()
        control.enableLocal(10)
        denied { flight.deliver { disclosures++ } }
        assertEquals(0, disclosures)
    }
    @Test fun flightAllowsOnlyOneEffectAndOneDelivery() {
        val control = policy()
        val flight = DebugFlight(control, control.enableLocal(10))
        var effects = 0
        var deliveries = 0
        assertTrue(flight.effect { effects++ })
        assertFalse(flight.effect { effects++ })
        assertTrue(flight.deliver { deliveries++ })
        assertFalse(flight.deliver { deliveries++ })
        assertEquals(1, effects)
        assertEquals(1, deliveries)
    }
    @Test fun cancelledAsyncFlightCannotDeliverOrDispatch() {
        val control = policy()
        val flight = DebugFlight(control, control.enableLocal(10))
        flight.cancel()
        assertFalse(flight.effect { fail("Cancelled dispatch") })
        assertFalse(flight.deliver { fail("Late callback") })
    }
    @Test fun unsupportedDurationsNeverGrantConsent() {
        val control = policy()
        for (minutes in listOf(0, 1, 11, 61, -1)) {
            try { control.enableLocal(minutes); fail("Invalid duration") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
        }
        assertFalse(control.status().enabled)
    }
}
