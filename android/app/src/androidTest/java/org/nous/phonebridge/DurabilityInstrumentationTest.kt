package org.nous.phonebridge

import android.Manifest
import android.content.Context
import android.content.Intent
import android.provider.AlarmClock
import androidx.test.ext.junit.runners.AndroidJUnit4
import androidx.test.platform.app.InstrumentationRegistry
import org.json.JSONArray
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
    @Test fun nativePermissionsAndUnsupportedFeaturesAreHonest() {
        val handlers = NativeHandlers(context, store)
        val caps = Access.capabilities(context).let { array -> (0 until array.length()).map { array.getString(it) }.toSet() }
        assertTrue("device.info" in caps)
        if (!Access.has(context, Manifest.permission.READ_CONTACTS)) {
            assertFalse("contacts.list" in caps)
            try { handlers.execute("contacts.list", JSONObject()); fail("Contacts permission required") } catch (e: BridgeFailure) { assertEquals("PERMISSION_REQUIRED", e.code) }
        }
        try { handlers.execute("shell.execute", JSONObject()); fail("No remote shell handler") } catch (e: BridgeFailure) { assertEquals("UNSUPPORTED", e.code) }
        assertTrue(GoogleClockAdapter().supported("unknown", 1).isEmpty())
        assertTrue(SamsungClockAdapter().supported("unknown", 1).isEmpty())
        assertTrue(AospClockAdapter().supported("unknown", 1).isEmpty())
    }
    @Test fun existingClockIntentsAreTypedAndNeverOwnClockSchedules() {
        val clocks = NativeClocks(context)
        val providers = clocks.installed()
        for (pkg in providers) {
            val action = Intent(AlarmClock.ACTION_SET_ALARM).setPackage(pkg)
            if (context.packageManager.resolveActivity(action, 0) != null) {
                val intent = clocks.intent("clock.alarms.create", Arguments(JSONObject().put("package", pkg).put("hour", 7).put("minute", 45).put("label", "CI typed intent").put("days", JSONArray(listOf(1, 7)))))
                assertEquals(AlarmClock.ACTION_SET_ALARM, intent.action)
                assertEquals(pkg, intent.`package`)
                assertEquals(7, intent.getIntExtra(AlarmClock.EXTRA_HOUR, -1))
                assertEquals(45, intent.getIntExtra(AlarmClock.EXTRA_MINUTES, -1))
                assertEquals(arrayListOf(2, 1), intent.getIntegerArrayListExtra(AlarmClock.EXTRA_DAYS))
                assertTrue(intent.getBooleanExtra(AlarmClock.EXTRA_SKIP_UI, false))
            }
        }
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
