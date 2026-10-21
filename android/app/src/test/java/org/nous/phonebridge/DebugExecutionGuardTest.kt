package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class DebugExecutionGuardTest {
    private class Fixture(initiallyEnabled: Boolean = true) {
        var elapsed = 0L
        var wall = 100L
        var connected = true
        var unlocked = true
        val control = DebugControl { elapsed }
        val session = if (initiallyEnabled) control.enableLocal(10) else "old-generation"
        val cache = linkedMapOf("command-1" to "screen-data")
        val cacheReads = mutableListOf<String>()
        val persisted = linkedMapOf<String, String>()
        val pending = ArrayDeque<String>()
        val sent = mutableListOf<String>()

        fun sinks(expected: String? = session): CommandSinks {
            ExecutionGuard.install(200L, { control.withSession(expected) {} }, { wall }) { connected }
            val captured = ExecutionGuard.snapshot()
            ExecutionGuard.clear()
            return CommandSinks(control, expected, captured, 200L, { wall }) {
                if (!unlocked) failure("PHONE_LOCKED", "Locked")
            }
        }

        fun record(stage: String, boundary: CommandSinks, prepare: () -> Unit = {}): String? = when (stage) {
            "cache" -> boundary.prepared({ prepare(); "command-1" }) { id ->
                cacheReads.add(id)
                cache[id]
            }
            "persistence" -> boundary.prepared({ prepare(); "command-1" to "screen-data" }) { (id, text) ->
                persisted[id] = text
                text
            }
            "enqueue" -> boundary.prepared({ prepare(); "screen-data" }) { text ->
                pending.addLast(text)
                text
            }
            "send" -> boundary.prepared({ prepare(); "screen-data" }) { text ->
                sent.add(text)
                text
            }
            else -> error(stage)
        }

        fun assertNoSink() {
            assertTrue(cacheReads.isEmpty())
            assertTrue(persisted.isEmpty())
            assertTrue(pending.isEmpty())
            assertTrue(sent.isEmpty())
            assertEquals(mapOf("command-1" to "screen-data"), cache)
        }
    }

    private val stages = listOf("cache", "persistence", "enqueue", "send")

    private fun rejected(code: String, action: () -> Unit) {
        try { action(); fail("Unauthorized prepared sink accepted") } catch (e: BridgeFailure) { assertEquals(code, e.code) }
    }

    @Test fun authorizedPreparedSinksPreserveCachePayloadAndCorrelation() {
        val fixture = Fixture()
        val boundary = fixture.sinks()
        for (stage in stages) assertEquals("screen-data", fixture.record(stage, boundary))
        assertEquals(listOf("command-1"), fixture.cacheReads)
        assertEquals(mapOf("command-1" to "screen-data"), fixture.persisted)
        assertEquals(listOf("screen-data"), fixture.pending.toList())
        assertEquals(listOf("screen-data"), fixture.sent)
    }

    @Test fun missingOffMismatchedRevokedAndReenabledSessionsRejectEveryPreparedSink() {
        for (stage in stages) for (state in listOf("missing", "off", "mismatched", "revoked", "reenabled")) {
            val fixture = Fixture()
            val expected = when (state) {
                "missing" -> null
                "mismatched" -> "other-generation"
                else -> fixture.session
            }
            val boundary = fixture.sinks(expected)
            when (state) {
                "off", "revoked" -> fixture.control.revoke()
                "reenabled" -> { fixture.control.revoke(); fixture.control.enableLocal(10) }
            }
            rejected("PERMISSION_REQUIRED") { fixture.record(stage, boundary) }
            fixture.assertNoSink()
        }
    }

    @Test fun defaultOffControlRejectsEveryPreparedSink() {
        for (stage in stages) {
            val fixture = Fixture(false)
            rejected("PERMISSION_REQUIRED") { fixture.record(stage, fixture.sinks()) }
            fixture.assertNoSink()
        }
    }

    @Test fun exactLeaseDeadlineRejectsEveryPreparedSinkAfterThreadLocalClear() {
        for (stage in stages) {
            val fixture = Fixture()
            val boundary = fixture.sinks()
            fixture.elapsed = 600000L
            rejected("PERMISSION_REQUIRED") { fixture.record(stage, boundary) }
            fixture.assertNoSink()
        }
    }

    @Test fun preparationCrossingExactLeaseDeadlineRejectsEveryPreparedSink() {
        for (stage in stages) {
            val fixture = Fixture()
            val boundary = fixture.sinks()
            fixture.elapsed = 599999L
            rejected("PERMISSION_REQUIRED") {
                fixture.record(stage, boundary) { fixture.elapsed = 600000L }
            }
            fixture.assertNoSink()
        }
    }

    @Test fun preparationCrossingExactWallTtlRejectsEveryPreparedSink() {
        for (stage in stages) {
            val fixture = Fixture()
            val boundary = fixture.sinks()
            fixture.wall = 199L
            rejected("EXPIRED") { fixture.record(stage, boundary) { fixture.wall = 200L } }
            fixture.assertNoSink()
        }
    }

    @Test fun revokeAndReenableDuringPreparationCannotAuthorizeOldGeneration() {
        for (stage in stages) {
            val fixture = Fixture()
            val boundary = fixture.sinks()
            rejected("PERMISSION_REQUIRED") {
                fixture.record(stage, boundary) { fixture.control.revoke(); fixture.control.enableLocal(10) }
            }
            fixture.assertNoSink()
        }
    }

    @Test fun capturedConnectionAndUnlockedChecksRunAfterPreparation() {
        for (stage in stages) for (state in listOf("disconnected", "locked")) {
            val fixture = Fixture()
            val boundary = fixture.sinks()
            rejected(if (state == "locked") "PHONE_LOCKED" else "AMBIGUOUS_STATE") {
                fixture.record(stage, boundary) {
                    if (state == "locked") fixture.unlocked = false else fixture.connected = false
                }
            }
            fixture.assertNoSink()
        }
    }

    @Test fun unlockedCheckCannotAgeTheLeasePastTheFinalSessionCheck() {
        val fixture = Fixture()
        val boundary = CommandSinks(fixture.control, fixture.session, {}, unlockedCheck = { fixture.elapsed = 600000L })
        rejected("PERMISSION_REQUIRED") { fixture.record("send", boundary) }
        fixture.assertNoSink()
    }

    @Test fun executionCheckCannotAgeTheLeasePastTheFinalSessionCheck() {
        val fixture = Fixture()
        val boundary = CommandSinks(fixture.control, fixture.session, { fixture.elapsed = 600000L })
        rejected("PERMISSION_REQUIRED") { fixture.record("persistence", boundary) }
        fixture.assertNoSink()
    }

    @Test fun finalSessionPrerequisitesCannotAgeWallTtlPastPreparedSink() {
        for (stage in stages) {
            val fixture = Fixture()
            var entries = 0
            val sessions = object : DebugSessions {
                override fun <T> withSession(expected: String?, block: () -> T): T = fixture.control.withSession(expected) {
                    if (++entries == 2) fixture.wall = 200L
                    block()
                }
            }
            val boundary = CommandSinks(sessions, fixture.session, {}, 200L, { fixture.wall })
            rejected("EXPIRED") { fixture.record(stage, boundary) }
            fixture.assertNoSink()
        }
    }

    @Test fun capturedPermissionPreparationCannotAgeLeasePastPreparedSink() {
        for (stage in stages) {
            val fixture = Fixture()
            ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) {
                fixture.elapsed = 600000L
                true
            }
            val captured = ExecutionGuard.snapshot()
            ExecutionGuard.clear()
            val boundary = CommandSinks(fixture.control, fixture.session, captured, 200L, { fixture.wall })
            rejected("PERMISSION_REQUIRED") { fixture.record(stage, boundary) }
            fixture.assertNoSink()
        }
    }

    @Test fun capturedPermissionPredicateCannotAgeLeasePastSnapshotAfterThreadLocalClear() {
        val fixture = Fixture()
        fixture.elapsed = 599999L
        ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) {
            fixture.elapsed = 600000L
            true
        }
        val captured = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        rejected("PERMISSION_REQUIRED", captured)
    }

    @Test fun capturedPermissionPredicateCannotReplaceSnapshotGeneration() {
        val fixture = Fixture()
        ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) {
            fixture.control.revoke()
            fixture.control.enableLocal(10)
            true
        }
        val captured = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        rejected("PERMISSION_REQUIRED", captured)
    }

    @Test fun capturedPermissionPredicateCannotAgeWallTtlPastSnapshot() {
        val fixture = Fixture()
        ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) {
            fixture.wall = 200L
            true
        }
        val captured = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        rejected("EXPIRED", captured)
    }

    @Test fun capturedUiSinksRejectPermissionPredicateCrossingExactLeaseBeforeEveryEffect() {
        for (effect in listOf("set-text", "global-action", "dispatch-gesture", "window-screenshot")) {
            val fixture = Fixture()
            val effects = mutableListOf<String>()
            var permissionChecked = false
            fixture.elapsed = 599999L
            ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) {
                permissionChecked = true
                fixture.elapsed = 600000L
                true
            }
            val boundary = ExecutionGuard.snapshotSinks(fixture.control, fixture.session) {
                if (!fixture.unlocked) failure("PHONE_LOCKED", "Locked")
            }
            ExecutionGuard.clear()
            rejected("PERMISSION_REQUIRED") {
                boundary.prepared({ effect }) { effects.add(it) }
            }
            assertTrue(permissionChecked)
            assertEquals(600000L, fixture.elapsed)
            assertTrue(effects.isEmpty())
        }
    }

    @Test fun capturedUiSinksRefreshLeaseAndWallTtlAfterUnlockedPrerequisites() {
        for (effect in listOf("set-text", "global-action", "dispatch-gesture", "window-screenshot")) {
            for (clock in listOf("lease", "wall")) {
                val fixture = Fixture()
                val effects = mutableListOf<String>()
                ExecutionGuard.install(200L, { fixture.control.withSession(fixture.session) {} }, { fixture.wall }) { fixture.connected }
                val boundary = ExecutionGuard.snapshotSinks(fixture.control, fixture.session) {
                    if (clock == "lease") fixture.elapsed = 600000L else fixture.wall = 200L
                }
                ExecutionGuard.clear()
                rejected(if (clock == "lease") "PERMISSION_REQUIRED" else "EXPIRED") {
                    boundary.prepared({ effect }) { effects.add(it) }
                }
                assertTrue(effects.isEmpty())
            }
        }
    }

    @Test fun capturedUiSinksRetainAuthorizationAfterThreadLocalClear() {
        for (state in listOf("authorized", "missing", "off", "old-session", "disconnected", "locked", "ttl")) {
            val fixture = Fixture()
            val effects = mutableListOf<String>()
            val expected = if (state == "missing") null else fixture.session
            ExecutionGuard.install(200L, { fixture.control.withSession(expected) {} }, { fixture.wall }) { fixture.connected }
            val boundary = ExecutionGuard.snapshotSinks(fixture.control, expected) {
                if (!fixture.unlocked) failure("PHONE_LOCKED", "Locked")
            }
            ExecutionGuard.clear()
            val dispatch = {
                boundary.prepared({
                    when (state) {
                        "off" -> fixture.control.revoke()
                        "old-session" -> { fixture.control.revoke(); fixture.control.enableLocal(10) }
                        "disconnected" -> fixture.connected = false
                        "locked" -> fixture.unlocked = false
                        "ttl" -> fixture.wall = 200L
                    }
                    "prepared-effect"
                }) { effects.add(it); Unit }
            }
            if (state == "authorized") {
                dispatch()
                assertEquals(listOf("prepared-effect"), effects)
            } else {
                val code = when (state) {
                    "disconnected" -> "AMBIGUOUS_STATE"
                    "locked" -> "PHONE_LOCKED"
                    "ttl" -> "EXPIRED"
                    else -> "PERMISSION_REQUIRED"
                }
                rejected(code, dispatch)
                assertTrue(effects.isEmpty())
            }
        }
    }

    @Test fun uiSnapshotRetainsGenerationAfterWorkerThreadLocalClears() {
        val control = DebugControl { 0L }
        val session = control.enableLocal(10)
        ExecutionGuard.install(Long.MAX_VALUE, { control.withSession(session) {} }) { true }
        val callback = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        control.revoke()
        control.enableLocal(10)
        rejected("PERMISSION_REQUIRED", callback)
    }

    @Test fun uiSnapshotRetainsCommandTtl() {
        var wall = 199L
        ExecutionGuard.install(200L, wallSeconds = { wall }) { true }
        val callback = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        wall = 200L
        rejected("EXPIRED", callback)
    }

    @Test fun uiSnapshotRetainsConnectionStopGuard() {
        var connected = true
        ExecutionGuard.install(Long.MAX_VALUE) { connected }
        val callback = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        connected = false
        rejected("AMBIGUOUS_STATE", callback)
    }

    @Test fun exactLeaseExpiryRejectsLateScreenshotDelivery() {
        var elapsed = 0L
        val control = DebugControl { elapsed }
        val capture = DebugFlight(control, control.enableLocal(10))
        capture.effect { }
        elapsed = 600000L
        val deliveries = mutableListOf<String>()
        rejected("PERMISSION_REQUIRED") { capture.deliver { deliveries.add("image") } }
        assertTrue(deliveries.isEmpty())
    }

    @Test fun ordinaryPreparedSinksDoNotRequireDebugConsent() {
        val persisted = linkedMapOf<String, String>()
        val sent = mutableListOf<String>()
        val boundary = CommandSinks()
        val error = "command-1:PERMISSION_REQUIRED"
        boundary.prepared({ "command-1" to error }) { (id, text) -> persisted[id] = text }
        assertTrue(boundary.prepared({ error }) { sent.add(it) })
        assertEquals(mapOf("command-1" to error), persisted)
        assertEquals(listOf(error), sent)
    }
}
