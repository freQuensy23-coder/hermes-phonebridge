package org.nous.phonebridge

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.net.ConnectivityManager
import android.net.Network
import android.net.NetworkCapabilities
import android.os.Handler
import android.os.IBinder
import android.os.Looper
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.Response
import okhttp3.WebSocket
import okhttp3.WebSocketListener
import org.json.JSONObject
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.RejectedExecutionException
import java.util.concurrent.ScheduledThreadPoolExecutor
import java.util.concurrent.ThreadPoolExecutor
import java.util.concurrent.TimeUnit
import kotlin.math.min
import kotlin.random.Random

object ExecutionGuard {
    private val active = ThreadLocal<Pair<Long, () -> Boolean>>()
    fun install(expires: Long, permitted: () -> Boolean) { active.set(expires to permitted) }
    fun clear() { active.remove() }
    fun check() {
        if (Thread.currentThread().isInterrupted) failure("AMBIGUOUS_STATE", "Execution interrupted; no retry")
        active.get()?.let { (expires, permitted) ->
            if (!permitted()) failure("AMBIGUOUS_STATE", "Connection stopped or revoked during command")
            if (System.currentTimeMillis() / 1000 >= expires) failure("EXPIRED", "Command expired before side effect")
        }
    }
    fun snapshot(): () -> Unit {
        val value = active.get()
        return {
            if (value != null) {
                if (!value.second()) failure("AMBIGUOUS_STATE", "Connection stopped or revoked during UI action")
                if (System.currentTimeMillis() / 1000 >= value.first) failure("EXPIRED", "Command expired before UI action")
            }
        }
    }
}

class BridgeService : Service() {
    private val control = ScheduledThreadPoolExecutor(1).apply { removeOnCancelPolicy = true }
    private val commands = ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, ArrayBlockingQueue<Runnable>(64))
    private val prefs by lazy { getSharedPreferences("connection", MODE_PRIVATE) }
    private val credentials by lazy { Credentials(this) }
    private val store by lazy { DurableStore(this) }
    private val manager by lazy { getSystemService(ConnectivityManager::class.java) }
    private val client: OkHttpClient = BridgeHttp.client.newBuilder().callTimeout(0, TimeUnit.SECONDS).readTimeout(0, TimeUnit.SECONDS).pingInterval(25, TimeUnit.SECONDS).build()
    @Volatile private var active = true
    @Volatile private var socket: WebSocket? = null
    @Volatile private var connected = false
    private var attempt = 0
    @Volatile private var epoch = 0L
    private var retry: java.util.concurrent.ScheduledFuture<*>? = null
    private var helloState = ""
    private var sentEvents = mutableMapOf<String, Long>()
    private var pendingResults = ArrayDeque<Pair<Long, String>>()
    private var lastHello = 0L
    private var credits = 8.0
    private var creditTime = android.os.SystemClock.elapsedRealtime()
    private val network = object : ConnectivityManager.NetworkCallback() {
        override fun onAvailable(network: Network) { scheduleNetwork() }
        override fun onLost(network: Network) { scheduleNetwork() }
        override fun onCapabilitiesChanged(network: Network, capabilities: NetworkCapabilities) { scheduleNetwork() }
    }
    override fun onCreate() {
        super.onCreate()
        current = this
        getSystemService(NotificationManager::class.java).createNotificationChannel(NotificationChannel(CHANNEL, "PhoneBridge connection", NotificationManager.IMPORTANCE_LOW))
        startForeground(18940, notification("Starting"))
        store.recover()
        manager.registerDefaultNetworkCallback(network)
        control.scheduleWithFixedDelay({ if (active) { advertise(); flush() } }, 3, 3, TimeUnit.SECONDS)
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP || intent?.action == DISCONNECT) {
            halt(if (intent.action == DISCONNECT) "Disconnected" else "Stopped", intent.action == DISCONNECT)
            return START_NOT_STICKY
        }
        if (!prefs.getBoolean("enabled", false) || credentials.load() == null) {
            halt("Not paired or stopped", false)
            return START_NOT_STICKY
        }
        control.execute { connect() }
        return START_STICKY
    }
    override fun onBind(intent: Intent?): IBinder? = null
    internal fun interruptTransport() {
        if (!active || control.isShutdown) return
        control.execute {
            val ownEpoch = epoch
            socket?.cancel()
            lost(ownEpoch, false)
        }
    }
    private fun hasNetwork(): Boolean = manager.activeNetwork?.let { manager.getNetworkCapabilities(it)?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) } == true
    private fun scheduleNetwork() {
        if (!active || control.isShutdown) return
        control.execute {
            if (!hasNetwork()) {
                epoch++
                socket?.cancel()
                socket = null
                connected = false
                state("Waiting for network")
            } else if (!connected) {
                retry?.cancel(false)
                retry = null
                socket?.cancel()
                socket = null
                connect()
            }
        }
    }
    private fun connect() {
        if (!active || socket != null || !prefs.getBoolean("enabled", false)) return
        if (!hasNetwork()) { state("Waiting for network"); return }
        val value = credentials.load() ?: run { halt("Credentials unavailable", false); return }
        val ownEpoch = ++epoch
        state("Connecting")
        val request = Request.Builder().url(value.wsUrl).header("Authorization", "Bearer ${value.token}").build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            override fun onOpen(webSocket: WebSocket, response: Response) {
                control.execute {
                    if (!active || ownEpoch != epoch) { webSocket.cancel(); return@execute }
                    socket = webSocket
                    connected = true
                    attempt = 0
                    prefs.edit().putLong("connections", prefs.getLong("connections", 0) + 1).apply()
                    helloState = ""
                    sentEvents.clear()
                    pendingResults.clear()
                    advertise()
                    state("Connected")
                    flush()
                }
            }
            override fun onMessage(webSocket: WebSocket, text: String) {
                if (text.toByteArray().size > 65536) { webSocket.close(1009, "Frame too large"); return }
                if (!active || ownEpoch != epoch) return
                try {
                    val frame = JSONObject(text)
                    PayloadBounds.validate(frame, 65536)
                    when (frame.optString("type")) {
                        "ack" -> control.execute {
                            val id = Arguments(frame).string("id", 128)!!
                            store.ack(id)
                            sentEvents.remove(id)
                        }
                        "command" -> accept(frame, value.deviceId)
                        "revoked" -> control.execute { halt("Revoked by server", true) }

                    }
                } catch (_: Exception) { webSocket.close(1008, "Invalid frame") }
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, null)
                control.execute { lost(ownEpoch, code in setOf(1008, 4001, 4003, 4401, 4403)) }
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                control.execute { lost(ownEpoch, code in setOf(1008, 4001, 4003, 4401, 4403)) }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!control.isShutdown) control.execute { lost(ownEpoch, response?.code in setOf(401, 403)) }
            }
        })
    }
    private fun lost(ownEpoch: Long, revoked: Boolean) {
        if (!active || ownEpoch != epoch) return
        epoch++
        socket = null
        connected = false
        if (revoked) { halt("Authorization revoked; scan a new QR", true); return }
        state("Reconnecting")
        val ceiling = min(300000L, 1000L shl attempt.coerceAtMost(9))
        attempt++
        retry?.cancel(false)
        retry = control.schedule({ retry = null; connect() }, Random.nextLong(ceiling / 2, ceiling + 1), TimeUnit.MILLISECONDS)
    }
    private fun accept(frame: JSONObject, deviceId: String) {
        val args = Arguments(frame)
        args.only("type", "id", "op", "args", "expires_at")
        val id = args.string("id", 128)!!
        args.string("op", 128)
        requireBridge(frame.opt("args") is JSONObject, "Command args must be an object")
        args.long("expires_at", 1, Long.MAX_VALUE)
        try {
            commands.execute {
                if (!active) return@execute
                var reserved = false
                val result = try {
                    val cached = store.begin(frame)
                    if (cached != null) cached else {
                        reserved = true
                        ExecutionGuard.install(frame.getLong("expires_at")) { active && prefs.getBoolean("enabled", false) && credentials.load()?.deviceId == deviceId }
                        ExecutionGuard.check()
                        val data = NativeHandlers(this, store).execute(frame.getString("op"), frame.getJSONObject("args"))
                        PayloadBounds.validate(data)
                        Results.ok(id, data)
                    }
                } catch (e: BridgeFailure) { Results.error(id, e.code, e.message) }
                  catch (_: SecurityException) { Results.error(id, "PERMISSION_REQUIRED", "Android denied the operation; inspect granted permissions") }
                  catch (_: android.content.ActivityNotFoundException) { Results.error(id, "NOT_FOUND", "Resolved activity is no longer available") }
                  catch (_: InterruptedException) { Results.error(id, "AMBIGUOUS_STATE", "Execution interrupted; no replay") }
                  catch (_: Exception) { Results.error(id, "AMBIGUOUS_STATE", "Native operation failed; inspect phone state before issuing a new command") }
                  finally { ExecutionGuard.clear() }
                if (reserved && active) {
                    try { store.complete(id, result) } catch (_: Exception) { halt("Result persistence failed; connection stopped for safety", false); return@execute }
                }
                if (active && !control.isShutdown) control.execute {
                    if (pendingResults.size < 2048) pendingResults.addLast(frame.getLong("expires_at") to result.toString())
                    flush()
                }
            }
        } catch (_: RejectedExecutionException) {
            control.execute { send(Results.error(id, "QUEUE_FULL", "Serial command queue full; no execution")) }
        }
    }
    private fun advertise() {
        if (!connected || !active) return
        try {
            val hello = JSONObject().put("type", "hello").put("version", BuildConfig.VERSION_NAME).put("capabilities", Access.capabilities(this)).put("permissions", Access.permissions(this))
            val text = hello.toString()
            val now = android.os.SystemClock.elapsedRealtime()
            if ((text != helloState || now - lastHello >= 25000) && socket?.send(text) == true) { helloState = text; lastHello = now }
        } catch (_: Exception) { state("Permission discovery unavailable") }
    }
    private fun takeCredit(): Boolean {
        val now = android.os.SystemClock.elapsedRealtime()
        credits = min(8.0, credits + (now - creditTime) / 350.0)
        creditTime = now
        if (credits < 1.0) return false
        credits -= 1.0
        return true
    }
    private fun send(frame: JSONObject): Boolean = connected && active && (socket?.queueSize() ?: Long.MAX_VALUE) < 262144 && takeCredit() && socket?.send(frame.toString()) == true
    private fun flush() {
        if (!active || !connected) return
        var budget = 8
        while (pendingResults.isNotEmpty() && budget-- > 0 && (socket?.queueSize() ?: Long.MAX_VALUE) < 262144) {
            if (pendingResults.first().first <= System.currentTimeMillis() / 1000) { pendingResults.removeFirst(); continue }
            if (!takeCredit() || socket?.send(pendingResults.first().second) != true) break
            pendingResults.removeFirst()
        }
        for (frame in store.events()) {
            if (budget-- <= 0) break
            val id = frame.getString("id")
            if (android.os.SystemClock.elapsedRealtime() - (sentEvents[id] ?: -10000L) >= 10000 && send(frame)) sentEvents[id] = android.os.SystemClock.elapsedRealtime()
        }
    }
    private fun state(value: String) {
        prefs.edit().putString("status", value).apply()
        getSystemService(NotificationManager::class.java).notify(18940, notification(value))
    }
    private fun notification(status: String): Notification {
        val open = PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 2, Intent(this, BridgeService::class.java).setAction(STOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("PhoneBridge").setContentText(status).setContentIntent(open).setOngoing(true).setCategory(Notification.CATEGORY_SERVICE).addAction(Notification.Action.Builder(null, "Stop", stop).build()).build()
    }
    private fun halt(message: String, erase: Boolean) {
        active = false
        connected = false
        prefs.edit().putBoolean("enabled", false).putString("status", message).commit()
        socket?.cancel()
        socket = null
        commands.shutdownNow()
        if (erase) { credentials.clear(); store.clearAll() }
        Handler(Looper.getMainLooper()).post { stopForeground(STOP_FOREGROUND_REMOVE); stopSelf() }
    }
    override fun onDestroy() {
        active = false
        connected = false
        if (current === this) current = null
        try { manager.unregisterNetworkCallback(network) } catch (_: Exception) { }
        retry?.cancel(false)
        socket?.cancel()
        commands.shutdownNow()
        control.shutdownNow()
        client.dispatcher.cancelAll()
        super.onDestroy()
    }
    companion object {
        const val CHANNEL = "phonebridge.connection"
        const val STOP = "org.nous.phonebridge.STOP"
        const val DISCONNECT = "org.nous.phonebridge.DISCONNECT"
        @Volatile var current: BridgeService? = null
        fun start(context: Context) {
            requireBridge(Credentials(context).load() != null, "Pair a device first")
            context.getSharedPreferences("connection", MODE_PRIVATE).edit().putBoolean("enabled", true).commit()
            context.startForegroundService(Intent(context, BridgeService::class.java))
        }
        fun stop(context: Context, disconnect: Boolean = false) {
            context.getSharedPreferences("connection", MODE_PRIVATE).edit().putBoolean("enabled", false).putString("status", if (disconnect) "Disconnected" else "Stopped").commit()
            val service = current
            if (service != null) service.halt(if (disconnect) "Disconnected" else "Stopped", disconnect) else if (disconnect) {
                Credentials(context).clear()
                DurableStore(context).use { it.clearAll() }
            }
        }
        fun refresh() {
            current?.let { service -> if (!service.control.isShutdown) service.control.execute { service.advertise() } }
        }
        fun emit(context: Context, name: String, data: JSONObject) {
            if (!context.getSharedPreferences("connection", MODE_PRIVATE).getBoolean("enabled", false)) return
            try {
                DurableStore(context).use { it.event(name, data) }
                current?.let { service -> if (!service.control.isShutdown) service.control.execute { service.flush() } }
            } catch (_: Exception) { }
        }
    }
}

class RestoreReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) return
        if (context.getSharedPreferences("connection", Context.MODE_PRIVATE).getBoolean("enabled", false) && Credentials(context).load() != null) {
            try { context.startForegroundService(Intent(context, BridgeService::class.java)) } catch (_: Exception) {
                context.getSharedPreferences("connection", Context.MODE_PRIVATE).edit().putString("status", "Android blocked automatic restore; open PhoneBridge").apply()
            }
        }
    }
}
