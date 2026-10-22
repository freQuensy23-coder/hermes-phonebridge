package org.nous.phonebridge

import android.content.Context
import android.content.Intent
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONObject
import org.junit.After
import org.junit.Assert.*
import org.junit.Before
import org.junit.Test
import org.junit.runner.RunWith
import java.util.UUID

@RunWith(AndroidJUnit4::class)
class DurabilityInstrumentationTest {
    private val context = InstrumentationRegistry.getInstrumentation().targetContext
    private lateinit var store: DurableStore
    private fun command(id: String = UUID.randomUUID().toString(), expires: Long = System.currentTimeMillis() / 1000 + 60) = JSONObject().put("type", "command").put("id", id).put("op", "device.info").put("args", JSONObject()).put("expires_at", expires)
    @Before fun setup() {
        BridgeService.stop(context, true)
        store = DurableStore(context)
        store.clearAll()
    }
    @After fun cleanup() { store.clearAll(); store.close(); Credentials(context).clear() }
    @Test fun completedResultIsPersistedBeforeReplayWithoutExecutingTwice() {
        val frame = command()
        assertNull(store.begin(frame))
        val result = Results.ok(frame.getString("id"), JSONObject().put("value", 42))
        store.complete(frame.getString("id"), result)
        store.close()
        store = DurableStore(context)
        assertEquals(result.toString(), store.begin(frame)!!.toString())
        assertEquals(1, store.results().size)
    }
    @Test fun interruptedSideEffectBecomesAmbiguousAndCannotBeReplayed() {
        val frame = command()
        assertNull(store.begin(frame))
        store.close()
        store = DurableStore(context)
        store.recover()
        val result = store.begin(frame)!!
        assertFalse(result.getBoolean("ok"))
        assertEquals("AMBIGUOUS_STATE", result.getJSONObject("error").getString("code"))
    }
    @Test fun expiredCommandNeverReservesAnExecutableSideEffect() {
        val frame = command(expires = System.currentTimeMillis() / 1000 - 1)
        val result = store.begin(frame)!!
        assertEquals("EXPIRED", result.getJSONObject("error").getString("code"))
        assertEquals(result.toString(), store.begin(frame)!!.toString())
    }
    @Test fun idReuseWithDifferentArgumentsIsRefused() {
        val frame = command()
        assertNull(store.begin(frame))
        val changed = JSONObject(frame.toString()).put("args", JSONObject().put("other", true))
        try { store.begin(changed); fail("Payload conflict must be rejected") } catch (e: BridgeFailure) { assertEquals("VALIDATION_ERROR", e.code) }
    }
    @Test fun eventsSurviveDatabaseReopenAndOnlyExactAckRemovesThem() {
        val first = store.event("ci.event", JSONObject().put("value", 1))
        val second = store.event("ci.event", JSONObject().put("value", 2))
        store.close()
        store = DurableStore(context)
        assertEquals(setOf(first, second), store.events().map { it.getString("id") }.toSet())
        store.ack(first)
        assertEquals(listOf(second), store.events().map { it.getString("id") })
        store.ack("unknown")
        assertEquals(1, store.events().size)
    }
    @Test fun durableOutboxIsBoundedWithoutUnboundedEventGrowth() {
        repeat(520) { store.event("ci.bounded", JSONObject().put("index", it)) }
        assertEquals(512, store.events().size)
        assertEquals(8, store.events().first().getJSONObject("data").getInt("index"))
        assertTrue(store.events().sumOf { it.toString().toByteArray().size } <= 2097152)
    }
    @Test fun lostAcksAndCountEvictionBoundTheProductionSendWindowOnOneConnection() {
        val window = EventSendWindow()
        val oldest = store.event("ci.lost_ack", JSONObject().put("index", 0))
        window.flush(store.events(), 8, 10000) { true }
        assertEquals(setOf(oldest), window.trackedIds)
        repeat(530) { index ->
            store.event("ci.lost_ack", JSONObject().put("index", index + 1))
            val retained = store.events()
            window.flush(retained, 8, 20000L + index * 10000L) { true }
            assertTrue(window.trackedIds.size <= 512)
            assertTrue(retained.map { it.getString("id") }.toSet().containsAll(window.trackedIds))
        }
        assertEquals(512, store.events().size)
        assertFalse(oldest in window.trackedIds)
        assertTrue(store.events().none { it.getString("id") == oldest })
        assertFalse(window.trackedIds.isEmpty())
    }
    @Test fun lostAcksAndByteOrAgeEvictionPruneWithoutAnySendBudget() {
        val window = EventSendWindow()
        val oldest = store.event("ci.lost_ack", JSONObject().put("value", "old"))
        window.flush(store.events(), 8, 10000) { true }
        repeat(150) { store.event("ci.large", JSONObject().put("value", "x".repeat(14900))) }
        assertTrue(store.events().size < 151)
        assertTrue(store.events().none { it.getString("id") == oldest })
        window.flush(store.events(), 0, 10001) { fail("No send budget"); true }
        assertFalse(oldest in window.trackedIds)
        val aged = store.event("ci.aged", JSONObject())
        window.flush(listOf(store.events().single { it.getString("id") == aged }), 8, 20000) { true }
        store.writableDatabase.execSQL("UPDATE events SET created=? WHERE id=?", arrayOf<Any>(System.currentTimeMillis() / 1000 - 604801, aged))
        store.event("ci.age_eviction", JSONObject())
        window.flush(store.events(), 0, 20001) { fail("No send budget"); true }
        assertFalse(aged in window.trackedIds)
        assertTrue(store.events().none { it.getString("id") == aged })
    }
    @Test fun resultCacheBackpressureNeverEvictsAnUnexpiredExecution() {
        val first = command(expires = System.currentTimeMillis() / 1000 + 3600)
        assertNull(store.begin(first))
        store.complete(first.getString("id"), Results.ok(first.getString("id"), JSONObject()))
        repeat(2047) {
            val frame = command(expires = System.currentTimeMillis() / 1000 + 3600)
            assertNull(store.begin(frame))
            store.complete(frame.getString("id"), Results.ok(frame.getString("id"), JSONObject()))
        }
        try { store.begin(command()); fail("Unexpired cache cannot overflow") } catch (e: BridgeFailure) { assertEquals("QUEUE_FULL", e.code) }
        assertTrue(store.begin(first)!!.getBoolean("ok"))
    }
    @Test fun keystoreCiphertextTamperingClearsCredentials() {
        val value = DeviceCredentials("fixture-id", "A".repeat(43), "https://phone.example/", "wss://phone.example/v1/device/ws")
        val credentials = Credentials(context)
        credentials.save(value)
        assertEquals(value, credentials.load())
        val prefs = context.getSharedPreferences("credentials", Context.MODE_PRIVATE)
        prefs.edit().putString("ciphertext", "AAAA").commit()
        assertNull(credentials.load())
        assertTrue(prefs.all.isEmpty())
    }
    @Test fun advertisedCapabilitiesAreLimitedToScreenControlAndFiles() {
        val handlers = NativeHandlers(context)
        val caps = Access.capabilities(context).let { array -> (0 until array.length()).map { array.getString(it) }.toSet() }
        assertTrue(caps.containsAll(setOf("device.info", "files.receive")))
        if (BridgeAccessibility.current == null || !ScreenControl.enabled(context)) assertTrue(caps.none { it.startsWith("screen.") })
        for (op in listOf("shell.execute", "contacts.list", "sms.send", "clock.alarms.create", "calendar.delete", "notifications.list")) {
            try { handlers.execute(op, JSONObject()); fail("$op must not have a handler") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        }
        try { handlers.execute("files.receive", JSONObject()); fail("files.receive needs pairing credentials") } catch (e: BridgeFailure) { assertEquals("AMBIGUOUS_STATE", e.code) }
    }
    @Test fun stopAndDisconnectPersistAcrossRestoreReceiver() {
        val value = DeviceCredentials("fixture-id", "A".repeat(43), "https://phone.example/", "wss://phone.example/v1/device/ws")
        Credentials(context).save(value)
        context.getSharedPreferences("connection", Context.MODE_PRIVATE).edit().putBoolean("enabled", false).commit()
        RestoreReceiver().onReceive(context, Intent(Intent.ACTION_BOOT_COMPLETED))
        assertNull(BridgeService.current)
        BridgeService.stop(context, true)
        assertNull(Credentials(context).load())
        assertTrue(store.events().isEmpty())
    }
}
