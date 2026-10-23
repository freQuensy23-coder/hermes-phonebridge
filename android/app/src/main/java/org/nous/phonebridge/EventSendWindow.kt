package org.nous.phonebridge

import org.json.JSONObject

internal class EventSendWindow(private val capacity: Int = 512) {
    private val sentAt = mutableMapOf<String, Long>()
    internal val trackedIds: Set<String> get() = sentAt.keys.toSet()

    fun clear() = sentAt.clear()
    fun ack(id: String) { sentAt.remove(id) }
    fun flush(events: List<JSONObject>, budget: Int, now: Long, send: (JSONObject) -> Boolean) {
        val retained = events.take(capacity)
        sentAt.keys.retainAll(retained.map { it.getString("id") }.toSet())
        for (frame in retained.take(budget.coerceAtLeast(0))) {
            val id = frame.getString("id")
            if (now - (sentAt[id] ?: -10000L) >= 10000 && send(frame)) sentAt[id] = now
        }
    }
}
