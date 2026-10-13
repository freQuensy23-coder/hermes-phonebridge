package org.nous.phonebridge

import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test

class EventSendWindowTest {
    private fun event(id: String) = JSONObject().put("id", id)

    @Test fun lostAcknowledgementsDoNotRetainEvictedIdsAndNewEventsSendImmediately() {
        val window = EventSendWindow(2)
        val sent = mutableListOf<String>()
        val send: (JSONObject) -> Boolean = { sent.add(it.getString("id")); true }
        window.flush(listOf(event("first"), event("second")), 2, 10000, send)
        assertEquals(setOf("first", "second"), window.trackedIds)
        window.flush(listOf(event("second"), event("third")), 2, 10001, send)
        assertEquals(setOf("second", "third"), window.trackedIds)
        assertEquals(listOf("first", "second", "third"), sent)
        window.flush(listOf(event("second"), event("third")), 2, 20001, send)
        assertEquals(listOf("first", "second", "third", "second", "third"), sent)
    }

    @Test fun evictionPrunesEvenWhenResultBudgetOrSendCreditIsExhausted() {
        val window = EventSendWindow(2)
        window.flush(listOf(event("old")), 1, 10000) { true }
        window.flush(listOf(event("retained")), 0, 10001) { fail("No event budget"); true }
        assertTrue(window.trackedIds.isEmpty())
        window.flush(listOf(event("retained")), 1, 10002) { false }
        assertTrue(window.trackedIds.isEmpty())
        window.flush(listOf(event("retained")), 1, 10003) { true }
        assertEquals(setOf("retained"), window.trackedIds)
        window.ack("unknown")
        assertEquals(setOf("retained"), window.trackedIds)
        window.ack("retained")
        assertTrue(window.trackedIds.isEmpty())
    }

    @Test fun aLongConnectionWithoutAnyAckNeverExceedsTheWindowCap() {
        val window = EventSendWindow(2)
        repeat(2000) { index ->
            val events = listOf(event("$index"), event("${index + 1}"), event("${index + 2}"))
            window.flush(events, 8, 10000L + index) { true }
            assertTrue(window.trackedIds.size <= 2)
            assertTrue(events.take(2).map { it.getString("id") }.toSet().containsAll(window.trackedIds))
        }
        window.clear()
        assertTrue(window.trackedIds.isEmpty())
    }
}
