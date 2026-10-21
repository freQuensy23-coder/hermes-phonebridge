package org.nous.phonebridge

import java.util.UUID

data class DebugSnapshot(val enabled: Boolean, val debugSession: String?, val remainingSeconds: Long?)

interface DebugSessions {
    fun <T> withSession(expected: String?, block: () -> T): T
}

class CommandSinks(
    private val sessions: DebugSessions? = null,
    private val session: String? = null,
    private val executionCheck: () -> Unit = {},
    private val expires: Long = Long.MAX_VALUE,
    private val wallSeconds: () -> Long = { System.currentTimeMillis() / 1000 },
    private val unlockedCheck: () -> Unit = {}
) {
    fun <P, T> prepared(prepare: () -> P, sink: (P) -> T): T {
        val prepared = prepare()
        return if (sessions == null) sink(prepared) else sessions.withSession(session) {
            executionCheck()
            unlockedCheck()
            sessions.withSession(session) {
                if (wallSeconds() >= expires) failure("EXPIRED", "Command expired before access or side effect")
                sink(prepared)
            }
        }
    }
}

class DebugControl(private val elapsedRealtime: () -> Long) : DebugSessions {
    private var session: String? = null
    private var deadline: Long? = null

    @Synchronized fun enableLocal(minutes: Int?): String {
        requireBridge(minutes == null || minutes in setOf(10, 15, 60), "Unsupported debug duration")
        deadline = minutes?.let { elapsedRealtime() + it * 60000L }
        session = UUID.randomUUID().toString()
        return session!!
    }

    @Synchronized fun revoke() {
        session = null
        deadline = null
    }

    @Synchronized fun status(): DebugSnapshot {
        val now = elapsedRealtime()
        if (deadline?.let { now >= it } == true) revoke()
        return DebugSnapshot(session != null, session, if (session == null) 0L else deadline?.let { (it - now + 999L) / 1000L })
    }

    @Synchronized override fun <T> withSession(expected: String?, block: () -> T): T {
        val current = status()
        if (!current.enabled || expected == null || current.debugSession != expected) failure("PERMISSION_REQUIRED", "Enable experimental debug locally and use its current debug_session")
        return block()
    }
}

class DebugFlight(private val control: DebugControl, private val session: String) {
    private var cancelled = false
    private var dispatched = false
    private var delivered = false

    fun effect(block: () -> Unit): Boolean = control.withSession(session) {
        synchronized(this) {
            if (cancelled || dispatched) false else { dispatched = true; block(); true }
        }
    }

    fun deliver(block: () -> Unit): Boolean = control.withSession(session) {
        synchronized(this) {
            if (cancelled || delivered || !dispatched) false else { delivered = true; block(); true }
        }
    }

    @Synchronized fun cancel() { cancelled = true }
}
