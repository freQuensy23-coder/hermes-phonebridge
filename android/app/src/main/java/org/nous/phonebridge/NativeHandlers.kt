package org.nous.phonebridge

import android.Manifest
import android.app.ActivityManager
import android.content.ClipData
import android.content.ClipboardManager
import android.content.ContentUris
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.hardware.camera2.CameraCharacteristics
import android.hardware.camera2.CameraManager
import android.location.LocationManager
import android.media.AudioManager
import android.net.Uri
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import android.os.SystemClock
import android.provider.CalendarContract
import android.provider.ContactsContract
import android.provider.Telephony
import android.telephony.SmsManager
import org.json.JSONArray
import org.json.JSONObject
import java.util.TimeZone
import java.util.concurrent.Callable
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

object UiThread {
    fun <T> call(block: () -> T): T {
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
        ExecutionGuard.check()
        val guard = ExecutionGuard.snapshot()
        val task = FutureTask(Callable { guard(); block() })
        Handler(Looper.getMainLooper()).post(task)
        return try { task.get(8, TimeUnit.SECONDS) } catch (e: java.util.concurrent.ExecutionException) {
            throw (e.cause ?: e)
        } catch (_: java.util.concurrent.TimeoutException) {
            task.cancel(false)
            failure("AMBIGUOUS_STATE", "UI dispatch timed out; action will not be retried")
        }
    }
}

class NativeHandlers(private val context: Context, private val store: DurableStore) {
    fun execute(op: String, json: JSONObject): JSONObject {
        ExecutionGuard.check()
        PayloadBounds.validate(json)
        val args = Arguments(json)
        return when {
            op.startsWith("notifications.") -> (BridgeNotifications.current ?: failure("PERMISSION_REQUIRED", "Enable notification listener access")).handle(op, args)
            op.startsWith("screen.") -> (BridgeAccessibility.current ?: failure("PERMISSION_REQUIRED", "Enable Accessibility")).handle(op, args)
            op.startsWith("clock.") -> NativeClocks(context).execute(op, args)
            else -> when (op) {
                "device.info" -> { args.only(); deviceInfo() }
                "capabilities", "device.capabilities" -> { args.only(); JSONObject().put("capabilities", Access.capabilities(context)).put("permissions", Access.permissions(context)) }
                "apps.list" -> apps(args)
                "apps.open" -> openApp(args)
                "audio.get" -> { args.only(); audio() }
                "audio.set" -> audioSet(args)
                "contacts.list" -> contacts(args)
                "calendar.list" -> calendarList(args)
                "calendar.create" -> calendarCreate(args)
                "calendar.delete" -> calendarDelete(args)
                "clipboard.get", "clipboard.set" -> clipboard(op, args)
                "location.get" -> location(args)
                "torch.set" -> torch(args)
                "calls.dial" -> dial(args)
                "sms.list" -> smsList(args)
                "sms.send" -> smsSend(args)
                else -> failure("UNSUPPORTED", "Operation has no native handler")
            }
        }
    }
    private fun deviceInfo(): JSONObject {
        val memory = ActivityManager.MemoryInfo().also { context.getSystemService(ActivityManager::class.java).getMemoryInfo(it) }
        return JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("sdk", Build.VERSION.SDK_INT).put("android", Build.VERSION.RELEASE).put("version", BuildConfig.VERSION_NAME)
            .put("battery_percent", context.getSystemService(BatteryManager::class.java).getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)).put("memory_available", memory.availMem).put("permissions", Access.permissions(context)).put("capabilities", Access.capabilities(context))
    }
    private fun apps(args: Arguments): JSONObject {
        args.only("limit", "after")
        val after = args.string("after", 256, false) ?: ""
        val pm = context.packageManager
        val entries = pm.queryIntentActivities(Intent(Intent.ACTION_MAIN).addCategory(Intent.CATEGORY_LAUNCHER), 0).distinctBy { it.activityInfo.packageName }.sortedBy { it.activityInfo.packageName }.filter { it.activityInfo.packageName > after }
        val selected = entries.take(args.limit().coerceAtMost(50))
        return JSONObject().put("apps", JSONArray(selected.map {
            val pkg = it.activityInfo.packageName
            val info = pm.getPackageInfo(pkg, 0)
            JSONObject().put("package", pkg).put("name", it.loadLabel(pm).toString().take(256)).put("version", info.versionName ?: "").put("version_code", info.longVersionCode)
        })).put("visibility", "launchable_packages").put("next_after", if (entries.size > selected.size) selected.last().activityInfo.packageName else JSONObject.NULL)
    }
    private fun openApp(args: Arguments): JSONObject {
        args.only("package")
        val pkg = args.string("package", 256)!!
        val intent = context.packageManager.getLaunchIntentForPackage(pkg) ?: failure("NOT_FOUND", "No visible launcher for package")
        return launch(intent, pkg)
    }
    fun launch(intent: Intent, pkg: String? = intent.`package`): JSONObject {
        Access.foreground(context)
        UiThread.call {
            Access.foreground(context)
            val service = BridgeAccessibility.current
            (service ?: context).startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK))
        }
        return JSONObject().put("outcome", "dispatched").put("verified", false).put("package", pkg ?: JSONObject.NULL)
    }
    private fun audio(): JSONObject {
        val manager = context.getSystemService(AudioManager::class.java)
        return JSONObject().put("ringer_mode", manager.ringerMode).put("streams", JSONObject().apply {
            streams.forEach { (name, id) -> put(name, JSONObject().put("volume", manager.getStreamVolume(id)).put("max", manager.getStreamMaxVolume(id)).put("muted", manager.isStreamMute(id))) }
        })
    }
    private fun audioSet(args: Arguments): JSONObject {
        args.only("stream", "volume")
        val stream = streams[args.string("stream", 32)] ?: failure("VALIDATION_ERROR", "Use music, ring, notification, alarm or call stream")
        val manager = context.getSystemService(AudioManager::class.java)
        val volume = args.int("volume", 0, manager.getStreamMaxVolume(stream))
        if ((stream == AudioManager.STREAM_RING || stream == AudioManager.STREAM_NOTIFICATION) && !context.getSystemService(android.app.NotificationManager::class.java).isNotificationPolicyAccessGranted) {
            failure("PERMISSION_REQUIRED", "Grant Do Not Disturb access before changing ring or notification volume")
        }
        ExecutionGuard.check()
        manager.setStreamVolume(stream, volume, 0)
        val observed = manager.getStreamVolume(stream)
        return audio().put("verified", observed == volume).put("requested_volume", volume)
    }
    private fun contacts(args: Arguments): JSONObject {
        args.only("limit", "after_id")
        Access.need(context, Manifest.permission.READ_CONTACTS)
        val after = if (args.json.has("after_id")) args.long("after_id", 0, Long.MAX_VALUE) else 0
        val items = JSONArray()
        val projection = arrayOf(ContactsContract.CommonDataKinds.Phone._ID, ContactsContract.CommonDataKinds.Phone.CONTACT_ID, ContactsContract.CommonDataKinds.Phone.DISPLAY_NAME, ContactsContract.CommonDataKinds.Phone.NUMBER)
        var more = false
        context.contentResolver.query(ContactsContract.CommonDataKinds.Phone.CONTENT_URI, projection, "${projection[0]}>?", arrayOf(after.toString()), "${projection[0]} ASC")?.use { cursor ->
            while (cursor.moveToNext()) {
                if (items.length() >= args.limit().coerceAtMost(50)) { more = true; break }
                items.put(JSONObject().put("id", cursor.getLong(0)).put("contact_id", cursor.getLong(1)).put("name", cursor.getString(2)?.take(256) ?: "").put("number", cursor.getString(3)?.take(128) ?: ""))
            }
        } ?: failure("UNSUPPORTED", "Contacts provider unavailable")
        return JSONObject().put("contacts", items).put("next_after_id", if (more) items.getJSONObject(items.length() - 1).getLong("id") else JSONObject.NULL)
    }
    private fun calendarList(args: Arguments): JSONObject {
        args.only("start_ms", "end_ms", "limit")
        Access.need(context, Manifest.permission.READ_CALENDAR)
        val start = args.long("start_ms", 0, 4102444800000)
        val end = args.long("end_ms", start + 1, (start + 366L * 86400000).coerceAtMost(4102444800000))
        val uri = CalendarContract.Instances.CONTENT_URI.buildUpon().also { ContentUris.appendId(it, start); ContentUris.appendId(it, end) }.build()
        val fields = arrayOf(CalendarContract.Instances.EVENT_ID, CalendarContract.Instances.CALENDAR_ID, CalendarContract.Instances.TITLE, CalendarContract.Instances.BEGIN, CalendarContract.Instances.END, CalendarContract.Instances.EVENT_LOCATION)
        val items = JSONArray()
        var truncated = false
        context.contentResolver.query(uri, fields, null, null, "${CalendarContract.Instances.BEGIN} ASC")?.use { cursor ->
            while (cursor.moveToNext()) {
                if (items.length() >= args.limit().coerceAtMost(30)) { truncated = true; break }
                items.put(JSONObject().put("id", cursor.getLong(0)).put("calendar_id", cursor.getLong(1)).put("title", cursor.getString(2)?.take(512) ?: "").put("start_ms", cursor.getLong(3)).put("end_ms", cursor.getLong(4)).put("location", cursor.getString(5)?.take(512) ?: ""))
            }
        } ?: failure("UNSUPPORTED", "Calendar provider unavailable")
        val calendars = JSONArray()
        context.contentResolver.query(CalendarContract.Calendars.CONTENT_URI, arrayOf(CalendarContract.Calendars._ID, CalendarContract.Calendars.CALENDAR_DISPLAY_NAME, CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL), null, null, "${CalendarContract.Calendars._ID} ASC")?.use { cursor ->
            while (cursor.moveToNext() && calendars.length() < 30) calendars.put(JSONObject().put("id", cursor.getLong(0)).put("name", cursor.getString(1)?.take(256) ?: "").put("writable", cursor.getInt(2) >= CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR))
        }
        return JSONObject().put("events", items).put("calendars", calendars).put("truncated", truncated).put("calendar_limit", 30)
    }
    private fun calendarCreate(args: Arguments): JSONObject {
        args.only("calendar_id", "title", "start_ms", "end_ms", "timezone", "description", "location")
        Access.need(context, Manifest.permission.WRITE_CALENDAR)
        Access.need(context, Manifest.permission.READ_CALENDAR)
        val calendar = args.long("calendar_id", 1, Long.MAX_VALUE)
        context.contentResolver.query(ContentUris.withAppendedId(CalendarContract.Calendars.CONTENT_URI, calendar), arrayOf(CalendarContract.Calendars.CALENDAR_ACCESS_LEVEL), null, null, null)?.use {
            if (!it.moveToFirst()) failure("NOT_FOUND", "Calendar not found")
            if (it.getInt(0) < CalendarContract.Calendars.CAL_ACCESS_CONTRIBUTOR) failure("PERMISSION_REQUIRED", "Calendar is read-only")
        } ?: failure("NOT_FOUND", "Calendar not found")
        val start = args.long("start_ms", 1, 4102444800000)
        val end = args.long("end_ms", start + 1, 4102444800000)
        val zone = args.string("timezone", 128, false) ?: TimeZone.getDefault().id
        requireBridge(zone in TimeZone.getAvailableIDs(), "Unknown timezone")
        val title = args.string("title", 512)!!
        val values = ContentValues().apply {
            put(CalendarContract.Events.CALENDAR_ID, calendar); put(CalendarContract.Events.TITLE, title)
            put(CalendarContract.Events.DTSTART, start); put(CalendarContract.Events.DTEND, end); put(CalendarContract.Events.EVENT_TIMEZONE, zone)
            args.string("description", 4096, false)?.let { put(CalendarContract.Events.DESCRIPTION, it) }
            args.string("location", 512, false)?.let { put(CalendarContract.Events.EVENT_LOCATION, it) }
        }
        ExecutionGuard.check()
        val uri = context.contentResolver.insert(CalendarContract.Events.CONTENT_URI, values) ?: failure("UNSUPPORTED", "Calendar rejected insertion")
        val id = ContentUris.parseId(uri)
        store.ownCalendar(id)
        val verified = context.contentResolver.query(uri, arrayOf(CalendarContract.Events.TITLE, CalendarContract.Events.DTSTART, CalendarContract.Events.DTEND), null, null, null)?.use { it.moveToFirst() && it.getString(0) == title && it.getLong(1) == start && it.getLong(2) == end } ?: false
        return JSONObject().put("id", id).put("verified", verified).put("outcome", if (verified) "created" else "dispatched")
    }
    private fun calendarDelete(args: Arguments): JSONObject {
        args.only("id", "expected_title")
        Access.need(context, Manifest.permission.WRITE_CALENDAR)
        Access.need(context, Manifest.permission.READ_CALENDAR)
        Access.unlocked(context)
        val id = args.long("id", 1, Long.MAX_VALUE)
        val expected = args.string("expected_title", 512)!!
        val uri = ContentUris.withAppendedId(CalendarContract.Events.CONTENT_URI, id)
        context.contentResolver.query(uri, arrayOf(CalendarContract.Events.TITLE), null, null, null)?.use {
            if (!it.moveToFirst()) failure("NOT_FOUND", "Event not found")
            requireBridge(it.getString(0) == expected, "Event title changed; deletion refused")
        } ?: failure("NOT_FOUND", "Event not found")
        ExecutionGuard.check()
        val count = context.contentResolver.delete(uri, "${CalendarContract.Events.TITLE}=?", arrayOf(expected))
        val absent = context.contentResolver.query(uri, arrayOf(CalendarContract.Events.DELETED), null, null, null)?.use { !it.moveToFirst() || it.getInt(0) != 0 } ?: false
        return JSONObject().put("id", id).put("deleted", count).put("verified", absent)
    }
    private fun clipboard(op: String, args: Arguments): JSONObject {
        args.only("text")
        Access.unlocked(context)
        if (!MainActivity.resumed) failure("FOREGROUND_REQUIRED", "Android clipboard requires PhoneBridge in foreground")
        return UiThread.call {
            Access.unlocked(context)
            if (!MainActivity.resumed) failure("FOREGROUND_REQUIRED", "Android clipboard requires PhoneBridge in foreground")
            val clipboard = context.getSystemService(ClipboardManager::class.java)
            if (op == "clipboard.set") {
                val clip = ClipData.newPlainText("PhoneBridge", args.string("text", 8192)!!)
                Access.unlocked(context)
                if (!MainActivity.resumed) failure("FOREGROUND_REQUIRED", "Android clipboard requires PhoneBridge in foreground")
                clipboard.setPrimaryClip(clip)
            }
            Access.unlocked(context)
            if (!MainActivity.resumed) failure("FOREGROUND_REQUIRED", "Android clipboard requires PhoneBridge in foreground")
            val clip = clipboard.primaryClip
            val text = if (clip != null && clip.itemCount > 0) clip.getItemAt(0).text?.toString()?.take(8192) else null
            JSONObject().put("text", text ?: JSONObject.NULL).put("verified", op != "clipboard.set" || text == args.string("text", 8192))
        }
    }
    private fun location(args: Arguments): JSONObject {
        args.only("max_age_seconds")
        if (!Access.has(context, Manifest.permission.ACCESS_FINE_LOCATION) && !Access.has(context, Manifest.permission.ACCESS_COARSE_LOCATION)) failure("PERMISSION_REQUIRED", "Grant location permission")
        if (!MainActivity.resumed) failure("FOREGROUND_REQUIRED", "Cached location requires foreground while-in-use permission")
        val maxAge = args.int("max_age_seconds", 1, 604800, 86400)
        val manager = context.getSystemService(LocationManager::class.java)
        val cached = manager.getProviders(true).mapNotNull { provider -> try { manager.getLastKnownLocation(provider) } catch (_: SecurityException) { null } }.maxByOrNull { it.elapsedRealtimeNanos } ?: failure("NOT_FOUND", "No last-known location; no active fix requested")
        val age = ((SystemClock.elapsedRealtimeNanos() - cached.elapsedRealtimeNanos) / 1000000000L).coerceAtLeast(0)
        if (age > maxAge) failure("NOT_FOUND", "Last-known location exceeds requested age")
        return JSONObject().put("latitude", cached.latitude).put("longitude", cached.longitude).put("accuracy_m", cached.accuracy.toDouble()).put("source", cached.provider).put("cached", true).put("age_seconds", age).put("time_ms", cached.time)
    }
    private fun torch(args: Arguments): JSONObject {
        args.only("enabled")
        Access.need(context, Manifest.permission.CAMERA)
        val enabled = args.bool("enabled")
        val manager = context.getSystemService(CameraManager::class.java)
        val id = manager.cameraIdList.firstOrNull { manager.getCameraCharacteristics(it).get(CameraCharacteristics.FLASH_INFO_AVAILABLE) == true } ?: failure("UNSUPPORTED", "No flash camera")
        ExecutionGuard.check()
        manager.setTorchMode(id, enabled)
        return JSONObject().put("outcome", "dispatched").put("verified", false).put("enabled", enabled).put("camera_id", id)
    }
    private fun number(args: Arguments, key: String): String {
        val number = args.string(key, 64)!!
        requireBridge(number.matches(Regex("\\+?[0-9][0-9 ()-]{0,62}")), "Use a phone number without URI, service codes or extensions")
        return number
    }
    private fun dial(args: Arguments): JSONObject {
        args.only("number")
        val intent = Intent(Intent.ACTION_DIAL, Uri.fromParts("tel", number(args, "number"), null))
        val resolved = context.packageManager.resolveActivity(intent, PackageManager.MATCH_DEFAULT_ONLY) ?: failure("UNSUPPORTED", "No dialer")
        intent.setPackage(resolved.activityInfo.packageName)
        return launch(intent, resolved.activityInfo.packageName)
    }
    private fun smsList(args: Arguments): JSONObject {
        args.only("limit", "before_id")
        Access.need(context, Manifest.permission.READ_SMS)
        val before = if (args.json.has("before_id")) args.long("before_id", 1, Long.MAX_VALUE) else Long.MAX_VALUE
        val fields = arrayOf(Telephony.Sms._ID, Telephony.Sms.ADDRESS, Telephony.Sms.BODY, Telephony.Sms.DATE, Telephony.Sms.TYPE)
        val items = JSONArray()
        var more = false
        context.contentResolver.query(Telephony.Sms.CONTENT_URI, fields, "${fields[0]}<?", arrayOf(before.toString()), "${fields[0]} DESC")?.use { cursor ->
            while (cursor.moveToNext()) {
                if (items.length() >= args.limit().coerceAtMost(20)) { more = true; break }
                items.put(JSONObject().put("id", cursor.getLong(0)).put("address", cursor.getString(1)?.take(128) ?: "").put("body", cursor.getString(2)?.take(1024) ?: "").put("date_ms", cursor.getLong(3)).put("type", cursor.getInt(4)).put("untrusted", true))
            }
        } ?: failure("UNSUPPORTED", "SMS provider unavailable")
        return JSONObject().put("messages", items).put("next_before_id", if (more) items.getJSONObject(items.length() - 1).getLong("id") else JSONObject.NULL)
    }
    private fun smsSend(args: Arguments): JSONObject {
        args.only("number", "text")
        Access.need(context, Manifest.permission.SEND_SMS)
        Access.unlocked(context)
        if (!context.packageManager.hasSystemFeature(PackageManager.FEATURE_TELEPHONY_MESSAGING)) failure("UNSUPPORTED", "No SMS modem")
        val number = number(args, "number")
        val text = args.string("text", 1600)!!
        requireBridge(text.isNotEmpty(), "SMS text cannot be empty")
        val manager = context.getSystemService(SmsManager::class.java) ?: failure("UNSUPPORTED", "SMS service unavailable")
        val parts = manager.divideMessage(text)
        requireBridge(parts.size <= 10, "SMS exceeds ten segments")
        ExecutionGuard.check()
        if (parts.size == 1) manager.sendTextMessage(number, null, text, null, null) else manager.sendMultipartTextMessage(number, null, parts, null, null)
        return JSONObject().put("outcome", "dispatched").put("verified", false).put("segments", parts.size).put("delivery", "not_observed")
    }
    companion object {
        val streams = mapOf("music" to AudioManager.STREAM_MUSIC, "ring" to AudioManager.STREAM_RING, "notification" to AudioManager.STREAM_NOTIFICATION, "alarm" to AudioManager.STREAM_ALARM, "call" to AudioManager.STREAM_VOICE_CALL)
    }
}
