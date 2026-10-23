package org.nous.phonebridge

import org.junit.Assert.*
import org.junit.Test

class ExecutionGuardTest {
    private fun rejected(code: String, action: () -> Unit) {
        try { action(); fail("Expected $code") } catch (e: BridgeFailure) { assertEquals(code, e.code) }
    }

    @Test fun snapshotKeepsConnectionGuardAfterThreadLocalClears() {
        var connected = true
        ExecutionGuard.install(Long.MAX_VALUE) { connected }
        val callback = ExecutionGuard.snapshot()
        ExecutionGuard.clear()
        callback()
        connected = false
        rejected("AMBIGUOUS_STATE", callback)
    }

    @Test fun expiredCommandIsRejected() {
        ExecutionGuard.install(System.currentTimeMillis() / 1000 - 1) { true }
        try { rejected("EXPIRED") { ExecutionGuard.check() } } finally { ExecutionGuard.clear() }
    }

    @Test fun noInstalledGuardAllowsDirectCalls() {
        ExecutionGuard.clear()
        ExecutionGuard.check()
        ExecutionGuard.snapshot()()
    }
}
