package org.nous.phonebridge

import android.app.ActivityManager
import android.content.Context
import android.os.BatteryManager
import android.os.Build
import android.os.Handler
import android.os.Looper
import org.json.JSONObject
import java.util.concurrent.Callable
import java.util.concurrent.FutureTask
import java.util.concurrent.TimeUnit

object UiThread {
    fun <T> call(block: () -> T): T {
        ExecutionGuard.check()
        if (Looper.myLooper() == Looper.getMainLooper()) return block()
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

class NativeHandlers(private val context: Context, private val credentials: DeviceCredentials? = null) {
    fun execute(op: String, json: JSONObject): JSONObject {
        ExecutionGuard.check()
        PayloadBounds.validate(json)
        val args = Arguments(json)
        return when {
            op.startsWith("screen.") -> {
                if (!ScreenControl.enabled(context)) failure("PERMISSION_REQUIRED", "Screen control is off; ask the owner to enable it in the PhoneBridge app")
                (BridgeAccessibility.current ?: failure("PERMISSION_REQUIRED", "Enable PhoneBridge Accessibility in Android settings")).handle(op, args)
            }
            op == "files.receive" -> FileReceiver(context, credentials ?: failure("AMBIGUOUS_STATE", "Pairing credentials unavailable")).receive(args)
            op == "device.info" -> { args.only(); deviceInfo() }
            op == "capabilities" || op == "device.capabilities" -> { args.only(); JSONObject().put("capabilities", Access.capabilities(context)).put("permissions", Access.permissions(context)) }
            else -> failure("UNSUPPORTED", "Operation $op is not supported by this app version")
        }
    }
    private fun deviceInfo(): JSONObject {
        val memory = ActivityManager.MemoryInfo().also { context.getSystemService(ActivityManager::class.java).getMemoryInfo(it) }
        return JSONObject().put("manufacturer", Build.MANUFACTURER).put("model", Build.MODEL).put("sdk", Build.VERSION.SDK_INT).put("android", Build.VERSION.RELEASE).put("version", BuildConfig.VERSION_NAME)
            .put("battery_percent", context.getSystemService(BatteryManager::class.java).getIntProperty(BatteryManager.BATTERY_PROPERTY_CAPACITY)).put("memory_available", memory.availMem).put("permissions", Access.permissions(context)).put("capabilities", Access.capabilities(context))
    }
}
