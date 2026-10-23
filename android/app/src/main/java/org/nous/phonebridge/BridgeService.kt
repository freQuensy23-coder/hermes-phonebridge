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
    private data class Guard(val expires: Long, val permitted: () -> Boolean) {
        fun check() {
            if (!permitted()) failure("AMBIGUOUS_STATE", "Connection stopped or revoked during command; no retry")
            if (System.currentTimeMillis() / 1000 >= expires) failure("EXPIRED", "Command expired before it ran")
        }
    }
    private val active = ThreadLocal<Guard>()
    fun install(expires: Long, permitted: () -> Boolean) { active.set(Guard(expires, permitted)) }
    fun clear() { active.remove() }
    fun check() {
        if (Thread.currentThread().isInterrupted) failure("AMBIGUOUS_STATE", "Execution interrupted; no retry")
        active.get()?.check()
    }
    fun snapshot(): () -> Unit {
        val value = active.get()
        return { value?.check() }
    }
}

class BridgeService : Service() {
    private val control = ScheduledThreadPoolExecutor(1).apply { removeOnCancelPolicy = true }
    private val commands = object : ThreadPoolExecutor(1, 1, 0, TimeUnit.SECONDS, ArrayBlockingQueue<Runnable>(64)) {
        override fun terminated() { store.close() }
    }
    private val prefs by lazy { getSharedPreferences("connection", MODE_PRIVATE) }
    private val credentials by lazy { Credentials(this) }
    private val store by lazy { DurableStore(this) }
    private val manager by lazy { getSystemService(ConnectivityManager::class.java) }
    private val client: OkHttpClient = BridgeHttp.client.newBuilder().dispatcher(okhttp3.Dispatcher()).connectionPool(okhttp3.ConnectionPool()).callTimeout(0, TimeUnit.SECONDS).readTimeout(0, TimeUnit.SECONDS).pingInterval(25, TimeUnit.SECONDS).build()
    @Volatile private var active = true
    @Volatile private var socket: WebSocket? = null
    @Volatile private var connected = false
    private var attempt = 0
    @Volatile private var epoch = 0L
    private var retry: java.util.concurrent.ScheduledFuture<*>? = null
    private var helloState = ""
    private val sentEvents = EventSendWindow()
    private data class PendingResult(val expires: Long, val id: String, val text: String)
    private val pendingResults = ArrayDeque<PendingResult>()
    private var pendingResultBytes = 0
    private var lastHello = 0L
    private var credits = 8.0
    private var creditTime = android.os.SystemClock.elapsedRealtime()
    private var networkUp = false
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
        control.scheduleWithFixedDelay({ dispatch { advertise(); flush() } }, 3, 3, TimeUnit.SECONDS)
    }
    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == STOP || intent?.action == DISCONNECT) {
            halt(if (intent.action == DISCONNECT) "Disconnected" else "Stopped", intent.action == DISCONNECT)
            return START_NOT_STICKY
        }
        if (!prefs.getBoolean("enabled", false) || !paired()) {
            halt("Not paired or stopped", false)
            return START_NOT_STICKY
        }
        dispatch { connect() }
        return START_STICKY
    }
    override fun onBind(intent: Intent?): IBinder? = null
    private fun paired(): Boolean = try { credentials.load() != null } catch (_: Exception) { true }
    private fun dispatch(task: () -> Unit) {
        if (!active || control.isShutdown) return
        try {
            control.execute {
                if (active) try { task() } catch (_: Exception) { try { recover("Internal error; reconnecting") } catch (_: Exception) { } }
            }
        } catch (_: RejectedExecutionException) { }
    }
    private fun recover(reason: String) {
        val ownEpoch = epoch
        socket?.cancel()
        lost(ownEpoch, reason)
    }
    internal fun interruptTransport() {
        if (!active || control.isShutdown) return
        dispatch { recover("Reconnecting") }
    }
    private fun hasNetwork(): Boolean = manager.activeNetwork?.let { manager.getNetworkCapabilities(it)?.hasCapability(NetworkCapabilities.NET_CAPABILITY_INTERNET) } == true
    private fun scheduleNetwork() {
        if (!active || control.isShutdown) return
        dispatch {
            val available = hasNetwork()
            val returned = available && !networkUp
            networkUp = available
            if (!available) {
                epoch++
                socket?.cancel()
                socket = null
                connected = false
                state("Waiting for network")
            } else if (returned && !connected && socket == null) {
                retry?.cancel(false)
                retry = null
                connect()
            }
        }
    }
    private fun connect() {
        if (!active || socket != null || !prefs.getBoolean("enabled", false)) return
        if (!hasNetwork()) { state("Waiting for network"); return }
        val value = credentials.load() ?: run { halt("Not paired; scan a new QR", false); return }
        val ownEpoch = ++epoch
        state("Connecting")
        val request = Request.Builder().url(value.wsUrl).header("Authorization", "Bearer ${value.token}").build()
        socket = client.newWebSocket(request, object : WebSocketListener() {
            private var serverError: String? = null
            override fun onOpen(webSocket: WebSocket, response: Response) {
                dispatch {
                    if (!active || ownEpoch != epoch) { webSocket.cancel(); return@dispatch }
                    socket = webSocket
                    connected = true
                    attempt = 0
                    prefs.edit().putLong("connections", prefs.getLong("connections", 0) + 1).apply()
                    helloState = ""
                    sentEvents.clear()
                    pendingResults.clear()
                    pendingResultBytes = 0
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
                        "ack" -> dispatch {
                            val id = Arguments(frame).string("id", 128)!!
                            store.ack(id)
                            sentEvents.ack(id)
                        }
                        "command" -> accept(frame, value)
                        "revoked" -> dispatch { halt("Unpaired by the server; scan a new QR", true) }
                        "error" -> { serverError = frame.optString("code", "ERROR").take(64) }
                    }
                } catch (_: Exception) { webSocket.close(1008, "Invalid frame") }
            }
            override fun onClosing(webSocket: WebSocket, code: Int, reason: String) {
                webSocket.close(code, null)
                closed(ownEpoch, code)
            }
            override fun onClosed(webSocket: WebSocket, code: Int, reason: String) {
                closed(ownEpoch, code)
            }
            private fun closed(ownEpoch: Long, code: Int) {
                dispatch {
                    if (ownEpoch != epoch) return@dispatch
                    if (code == 4401) halt("Unpaired by the server; scan a new QR", true)
                    else lost(ownEpoch, serverError?.let { "Server closed the connection ($it); reconnecting" } ?: "Reconnecting")
                }
            }
            override fun onFailure(webSocket: WebSocket, t: Throwable, response: Response?) {
                if (!control.isShutdown) dispatch { lost(ownEpoch, response?.code?.let { "Server unavailable (HTTP $it); reconnecting" } ?: "Server unreachable; reconnecting") }
            }
        })
    }
    private fun lost(ownEpoch: Long, reason: String) {
        if (!active || ownEpoch != epoch) return
        epoch++
        socket = null
        connected = false
        state(reason)
        val ceiling = min(300000L, 1000L shl attempt.coerceAtMost(9))
        attempt++
        retry?.cancel(false)
        retry = control.schedule({ retry = null; dispatch { connect() } }, Random.nextLong(ceiling / 2, ceiling + 1), TimeUnit.MILLISECONDS)
    }
    private fun accept(frame: JSONObject, paired: DeviceCredentials) {
        val deviceId = paired.deviceId
        val args = Arguments(frame)
        args.only("type", "id", "op", "args", "expires_at")
        val id = args.string("id", 128)!!
        val op = args.string("op", 128)!!
        requireBridge(frame.opt("args") is JSONObject, "Command args must be an object")
        val expires = args.long("expires_at", 1, Long.MAX_VALUE)
        val acceptedEpoch = epoch
        try {
            commands.execute {
                if (!active) return@execute
                var result: JSONObject
                try {
                    ExecutionGuard.install(expires) { active && connected && epoch == acceptedEpoch && prefs.getBoolean("enabled", false) && credentials.load()?.deviceId == deviceId }
                    var reserved = false
                    result = try {
                        ExecutionGuard.check()
                        val cached = store.begin(frame)
                        reserved = cached == null
                        cached ?: Results.ok(id, NativeHandlers(this, paired).execute(op, frame.getJSONObject("args"))).also { PayloadBounds.validateResult(it, op) }
                    } catch (e: BridgeFailure) { Results.error(id, e.code, e.message) }
                      catch (_: SecurityException) { Results.error(id, "PERMISSION_REQUIRED", "Android denied the operation; inspect granted permissions") }
                      catch (_: android.content.ActivityNotFoundException) { Results.error(id, "NOT_FOUND", "Resolved activity is no longer available") }
                      catch (_: InterruptedException) { Results.error(id, "AMBIGUOUS_STATE", "Execution interrupted; no replay") }
                      catch (_: Exception) { Results.error(id, "AMBIGUOUS_STATE", "Native operation failed; inspect phone state before issuing a new command") }
                    if (reserved && active) store.complete(id, result, op)
                } catch (_: Exception) {
                    halt("Result persistence failed; connection stopped for safety", false)
                    return@execute
                } finally { ExecutionGuard.clear() }
                if (active && !control.isShutdown) dispatch {
                    addPending(PendingResult(expires, id, result.toString()))
                    flush()
                }
            }
        } catch (_: RejectedExecutionException) {
            dispatch {
                addPending(PendingResult(expires, id, Results.error(id, "QUEUE_FULL", "Serial command queue full; no execution").toString()))
                flush()
            }
        }
    }
    private fun addPending(result: PendingResult) {
        val bytes = result.text.toByteArray().size
        if (pendingResults.size >= 2048 || pendingResultBytes + bytes > 2097152) {
            recover("Too many unsent results; reconnecting to resend from cache")
            return
        }
        pendingResults.addLast(result)
        pendingResultBytes += bytes
    }
    private fun removePending() {
        pendingResultBytes -= pendingResults.removeFirst().text.toByteArray().size
    }
    private fun advertise() {
        if (!connected || !active) return
        try {
            val hello = JSONObject().put("type", "hello").put("version", BuildConfig.VERSION_NAME).put("capabilities", Access.capabilities(this)).put("permissions", Access.permissions(this))
            val text = hello.toString()
            val now = android.os.SystemClock.elapsedRealtime()
            if ((text != helloState || now - lastHello >= 25000) && socket?.send(text) == true) {
                if (text != helloState) getSystemService(NotificationManager::class.java).notify(18940, notification(prefs.getString("status", "Connected") ?: "Connected"))
                helloState = text; lastHello = now
            }
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
            val pending = pendingResults.first()
            if (pending.expires <= System.currentTimeMillis() / 1000) { removePending(); continue }
            if (!takeCredit()) break
            if (socket?.send(pending.text) != true) break
            removePending()
        }
        sentEvents.flush(store.events(), budget, android.os.SystemClock.elapsedRealtime()) { send(it) }
    }
    private fun state(value: String) {
        prefs.edit().putString("status", value).apply()
        getSystemService(NotificationManager::class.java).notify(18940, notification(value))
    }
    private fun notification(status: String): Notification {
        val open = PendingIntent.getActivity(this, 1, Intent(this, MainActivity::class.java), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        val stop = PendingIntent.getService(this, 2, Intent(this, BridgeService::class.java).setAction(STOP), PendingIntent.FLAG_IMMUTABLE or PendingIntent.FLAG_UPDATE_CURRENT)
        return Notification.Builder(this, CHANNEL).setSmallIcon(android.R.drawable.stat_notify_sync).setContentTitle("PhoneBridge").setContentText(if (ScreenControl.enabled(this)) "${ScreenControl.describe(this)} · $status" else status).setContentIntent(open).setOngoing(true).setOnlyAlertOnce(true).setCategory(Notification.CATEGORY_SERVICE)
            .addAction(Notification.Action.Builder(null, "Stop connection", stop).build()).build()
    }
    private fun halt(message: String, erase: Boolean) {
        active = false
        connected = false
        ScreenControl.stop(this)
        prefs.edit().putBoolean("enabled", false).putString("status", message).commit()
        socket?.cancel()
        socket = null
        if (erase) { credentials.clear(); store.clearAll(); ScreenControl.stop(this) }
        commands.shutdownNow()
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
            ScreenControl.stop(context)
            context.getSharedPreferences("connection", MODE_PRIVATE).edit().putBoolean("enabled", false).putString("status", if (disconnect) "Disconnected" else "Stopped").commit()
            val service = current
            if (service != null) service.halt(if (disconnect) "Disconnected" else "Stopped", disconnect) else if (disconnect) {
                Credentials(context).clear()
                DurableStore(context).use { it.clearAll() }
                ScreenControl.stop(context)
            }
        }
        fun refresh() {
            current?.let { service -> if (!service.control.isShutdown) service.dispatch {
                service.advertise()
                service.getSystemService(NotificationManager::class.java).notify(18940, service.notification(service.prefs.getString("status", "Connected") ?: "Connected"))
                service.flush()
            } }
        }
        fun emit(context: Context, name: String, data: JSONObject) {
            if (!context.getSharedPreferences("connection", MODE_PRIVATE).getBoolean("enabled", false)) return
            try {
                DurableStore(context).use { it.event(name, data) }
                current?.let { service -> if (!service.control.isShutdown) service.dispatch { service.flush() } }
            } catch (_: Exception) { }
        }
    }
}

class RestoreReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent) {
        if (intent.action !in setOf(Intent.ACTION_BOOT_COMPLETED, Intent.ACTION_MY_PACKAGE_REPLACED)) return
        if (!context.getSharedPreferences("connection", Context.MODE_PRIVATE).getBoolean("enabled", false)) return
        if (try { Credentials(context).load() == null } catch (_: Exception) { false }) return
        run {
            try { context.startForegroundService(Intent(context, BridgeService::class.java)) } catch (_: Exception) {
                context.getSharedPreferences("connection", Context.MODE_PRIVATE).edit().putString("status", "Android blocked automatic restore; open PhoneBridge").apply()
            }
        }
    }
}
