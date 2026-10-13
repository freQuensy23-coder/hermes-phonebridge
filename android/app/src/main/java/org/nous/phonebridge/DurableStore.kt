package org.nous.phonebridge

import android.content.ContentValues
import android.content.Context
import android.database.sqlite.SQLiteDatabase
import android.database.sqlite.SQLiteOpenHelper
import org.json.JSONObject
import java.security.MessageDigest
import java.util.UUID

class DurableStore(context: Context) : SQLiteOpenHelper(context, "bridge.sqlite3", null, 1) {
    init { setWriteAheadLoggingEnabled(true) }
    override fun onCreate(db: SQLiteDatabase) {
        db.execSQL("CREATE TABLE commands(id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, expires INTEGER NOT NULL, result TEXT, created INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE events(id TEXT PRIMARY KEY, payload TEXT NOT NULL, created INTEGER NOT NULL)")
        db.execSQL("CREATE TABLE owned_calendar(id INTEGER PRIMARY KEY)")
    }
    override fun onUpgrade(db: SQLiteDatabase, oldVersion: Int, newVersion: Int) = Unit
    @Synchronized fun recover() {
        val db = writableDatabase
        db.query("commands", arrayOf("id"), "result IS NULL", null, null, null, null).use { cursor ->
            while (cursor.moveToNext()) {
                val id = cursor.getString(0)
                db.update("commands", ContentValues().apply { put("result", Results.error(id, "AMBIGUOUS_STATE", "Execution was interrupted; no side effect was replayed").toString()) }, "id=?", arrayOf(id))
            }
        }
    }
    @Synchronized fun begin(command: JSONObject): JSONObject? {
        val args = Arguments(command)
        val id = args.string("id", 128)!!
        requireBridge(id.isNotBlank(), "Empty command ID")
        val now = System.currentTimeMillis() / 1000
        val expires = args.long("expires_at", 1, Long.MAX_VALUE)
        val body = JSONObject().put("op", command.getString("op")).put("args", command.getJSONObject("args")).put("expires_at", expires)
        val digest = MessageDigest.getInstance("SHA-256").digest(canonical(body).toByteArray()).joinToString("") { "%02x".format(it) }
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.query("commands", arrayOf("fingerprint", "result"), "id=?", arrayOf(id), null, null, null).use { cursor ->
                if (cursor.moveToFirst()) {
                    requireBridge(cursor.getString(0) == digest, "Command ID reused with different payload")
                    val cached = if (cursor.isNull(1)) Results.error(id, "AMBIGUOUS_STATE", "Command already started; replay refused") else JSONObject(cursor.getString(1))
                    db.setTransactionSuccessful()
                    return cached
                }
            }
            db.delete("commands", "expires<? AND result IS NOT NULL", arrayOf((now - 604800).toString()))
            val count = db.rawQuery("SELECT COUNT(*) FROM commands", null).use { it.moveToFirst(); it.getInt(0) }
            if (count >= 2048) {
                db.delete("commands", "id IN (SELECT id FROM commands WHERE expires<? AND result IS NOT NULL ORDER BY created LIMIT 128)", arrayOf(now.toString()))
                val remaining = db.rawQuery("SELECT COUNT(*) FROM commands", null).use { it.moveToFirst(); it.getInt(0) }
                if (remaining >= 2048) failure("QUEUE_FULL", "Result cache full; command was not executed")
            }
            val result = if (expires <= now) Results.error(id, "EXPIRED", "Command expired before execution") else null
            db.insertOrThrow("commands", null, ContentValues().apply {
                put("id", id); put("fingerprint", digest); put("expires", expires); put("created", now)
                if (result != null) put("result", result.toString()) else putNull("result")
            })
            db.setTransactionSuccessful()
            return result
        } finally { db.endTransaction() }
    }
    @Synchronized fun complete(id: String, result: JSONObject) {
        requireBridge(result.toString().toByteArray().size <= 120000, "Result exceeds transport bound")
        check(writableDatabase.update("commands", ContentValues().apply { put("result", result.toString()) }, "id=? AND result IS NULL", arrayOf(id)) == 1)
    }
    @Synchronized fun results(): List<JSONObject> = readableDatabase.query("commands", arrayOf("result"), "result IS NOT NULL", null, null, null, "created DESC", "2048").use { cursor ->
        buildList { while (cursor.moveToNext()) add(JSONObject(cursor.getString(0))) }
    }
    @Synchronized fun event(name: String, data: JSONObject): String {
        requireBridge(name.length in 1..128, "Invalid event name")
        val id = UUID.randomUUID().toString()
        val now = System.currentTimeMillis() / 1000
        val frame = JSONObject().put("type", "event").put("id", id).put("name", name).put("data", data).put("time", now).toString()
        requireBridge(frame.toByteArray().size <= 16384, "Event too large")
        val db = writableDatabase
        db.beginTransaction()
        try {
            db.insertOrThrow("events", null, ContentValues().apply { put("id", id); put("payload", frame); put("created", now) })
            db.delete("events", "created<?", arrayOf((now - 604800).toString()))
            db.execSQL("DELETE FROM events WHERE id NOT IN (SELECT id FROM events ORDER BY created DESC, rowid DESC LIMIT 512)")
            while (db.rawQuery("SELECT COALESCE(SUM(length(CAST(payload AS BLOB))),0) FROM events", null).use { it.moveToFirst(); it.getLong(0) } > 2097152) {
                db.execSQL("DELETE FROM events WHERE id=(SELECT id FROM events ORDER BY created,rowid LIMIT 1)")
            }
            db.setTransactionSuccessful()
        } finally { db.endTransaction() }
        return id
    }
    @Synchronized fun events(): List<JSONObject> = readableDatabase.query("events", arrayOf("payload"), null, null, null, null, "created,rowid", "512").use { cursor ->
        buildList { while (cursor.moveToNext()) add(JSONObject(cursor.getString(0))) }
    }
    @Synchronized fun ack(id: String) { writableDatabase.delete("events", "id=?", arrayOf(id)) }
    @Synchronized fun ownCalendar(id: Long) { writableDatabase.insertWithOnConflict("owned_calendar", null, ContentValues().apply { put("id", id) }, SQLiteDatabase.CONFLICT_IGNORE) }
    @Synchronized fun isOwnCalendar(id: Long): Boolean = readableDatabase.rawQuery("SELECT id FROM owned_calendar WHERE id=?", arrayOf(id.toString())).use { it.moveToFirst() }
    @Synchronized fun clearAll() {
        writableDatabase.beginTransaction()
        try {
            for (table in listOf("commands", "events", "owned_calendar")) writableDatabase.delete(table, null, null)
            writableDatabase.setTransactionSuccessful()
        } finally { writableDatabase.endTransaction() }
    }
    companion object {
        fun canonical(value: Any?): String = when (value) {
            is JSONObject -> value.keys().asSequence().toList().sorted().joinToString(prefix = "{", postfix = "}") { JSONObject.quote(it) + ":" + canonical(value.get(it)) }
            is org.json.JSONArray -> (0 until value.length()).joinToString(prefix = "[", postfix = "]") { canonical(value.get(it)) }
            is String -> JSONObject.quote(value)
            else -> value.toString()
        }
    }
}
