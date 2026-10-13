package org.nous.phonebridge

import android.app.Notification
import android.app.PendingIntent
import android.app.RemoteInput
import android.content.Intent
import android.service.notification.NotificationListenerService
import android.service.notification.StatusBarNotification
import org.json.JSONArray
import org.json.JSONObject

class BridgeNotifications : NotificationListenerService() {
    override fun onListenerConnected() {
        current = this
        BridgeService.refresh()
    }
    override fun onListenerDisconnected() {
        if (current === this) current = null
        BridgeService.refresh()
    }
    override fun onDestroy() {
        if (current === this) current = null
        BridgeService.refresh()
        super.onDestroy()
    }
    override fun onNotificationPosted(sbn: StatusBarNotification) {
        if (sbn.packageName != packageName) BridgeService.emit(applicationContext, "notification.posted", describe(sbn))
    }
    override fun onNotificationRemoved(sbn: StatusBarNotification) {
        if (sbn.packageName != packageName) BridgeService.emit(applicationContext, "notification.removed", JSONObject().put("key", sbn.key).put("package", sbn.packageName))
    }
    fun handle(op: String, args: Arguments): JSONObject {
        if (op == "notifications.list") {
            args.only("limit")
            val active = activeNotifications.orEmpty()
            val items = JSONArray()
            for (notification in active.take(args.limit().coerceAtMost(8))) {
                val candidate = describe(notification)
                val next = JSONArray(items.toString()).put(candidate)
                try { PayloadBounds.validate(JSONObject().put("notifications", next), 28000) } catch (_: BridgeFailure) { break }
                items.put(candidate)
            }
            return JSONObject().put("notifications", items).put("truncated", active.size > items.length()).put("untrusted", true)
        }
        args.only("key", "action", "text", "result_key")
        ExecutionGuard.check()
        val key = args.string("key", 512)!!
        val notification = activeNotifications.orEmpty().firstOrNull { it.key == key } ?: failure("NOT_FOUND", "Notification no longer exists")
        when (op) {
            "notifications.dismiss" -> {
                requireBridge(notification.isClearable, "Notification is not dismissible")
                ExecutionGuard.check()
                cancelNotification(key)
            }
            "notifications.action", "notifications.reply" -> {
                Access.unlocked(this)
                val actions = notification.notification.actions.orEmpty()
                val actionIndex = args.int("action", 0, (actions.size - 1).coerceAtLeast(0))
                val action = actions.getOrNull(actionIndex) ?: failure("NOT_FOUND", "Exact notification action no longer exists")
                val fill = Intent()
                if (op == "notifications.reply") {
                    val text = args.string("text", 4096)!!
                    val keyArg = args.string("result_key", 128, false)
                    val inputs = action.remoteInputs.orEmpty().filter { it.allowFreeFormInput }
                    val input = if (keyArg != null) inputs.firstOrNull { it.resultKey == keyArg } else inputs.singleOrNull()
                    if (input == null) failure("VALIDATION_ERROR", "Select an exact free-form RemoteInput result_key")
                    RemoteInput.addResultsToIntent(arrayOf(input), fill, android.os.Bundle().apply { putCharSequence(input.resultKey, text) })
                    RemoteInput.setResultsSource(fill, RemoteInput.SOURCE_FREE_FORM_INPUT)
                }
                ExecutionGuard.check()
                try { action.actionIntent.send(this, 0, fill) } catch (_: PendingIntent.CanceledException) { failure("NOT_FOUND", "Notification action expired") }
            }
            else -> failure("UNSUPPORTED", "Unknown notification operation")
        }
        return JSONObject().put("outcome", "dispatched").put("verified", false).put("key", key)
    }
    companion object {
        @Volatile var current: BridgeNotifications? = null
        fun describe(sbn: StatusBarNotification): JSONObject {
            val n = sbn.notification
            return JSONObject().put("key", sbn.key).put("package", sbn.packageName).put("post_time", sbn.postTime).put("clearable", sbn.isClearable).put("untrusted", true)
                .put("title", n.extras.getCharSequence(Notification.EXTRA_TITLE)?.toString()?.take(512) ?: "")
                .put("text", (n.extras.getCharSequence(Notification.EXTRA_BIG_TEXT) ?: n.extras.getCharSequence(Notification.EXTRA_TEXT))?.toString()?.take(2048) ?: "")
                .put("actions", JSONArray(n.actions.orEmpty().take(16).mapIndexed { index, action -> JSONObject().put("index", index).put("title", action.title?.toString()?.take(256) ?: "").put("remote_inputs", JSONArray(action.remoteInputs.orEmpty().map { JSONObject().put("result_key", it.resultKey).put("free_form", it.allowFreeFormInput) })) }))
        }
    }
}
