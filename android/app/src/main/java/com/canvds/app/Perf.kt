package com.canvds.app

import android.content.Context
import android.os.PerformanceHintManager
import android.os.Process

/**
 * Asks Android to keep the threads that move CAN frames fast and steady:
 *  - raises the Linux priority of each registered thread (usb pump, Python rx/tx threads)
 *  - Android 12+ ADPF hint session: tells the scheduler/governor these threads have a ~2 ms work cycle, so they are put on fast cores
 *    at a higher frequency (reportActualWorkDuration slightly above the target makes the governor lean towards "faster")
 * Called from Kotlin and from Python (Perf.boostThisThread() through Chaquopy's jclass).
 */
object Perf {
    private val tids = LinkedHashSet<Int>()
    private var hint: PerformanceHintManager? = null
    private var session: PerformanceHintManager.Session? = null
    private var reporter: Thread? = null
    @Volatile private var running = false
    @Volatile var wakeLock = false
    @Volatile var sustained = false
    @Volatile var lastError = ""

    private const val TARGET_NS = 2_000_000L
    private const val REPORT_NS = 3_000_000L

    @Synchronized fun init(ctx: Context) {
        if (hint == null && android.os.Build.VERSION.SDK_INT >= 31) {
            hint = ctx.getSystemService(Context.PERFORMANCE_HINT_SERVICE) as? PerformanceHintManager
        }
    }

    @JvmStatic fun boostThisThread() {
        try { Process.setThreadPriority(Process.THREAD_PRIORITY_URGENT_AUDIO) } catch (e: Throwable) { lastError = "优先级: $e" }
        addTid(Process.myTid())
    }

    @Synchronized private fun addTid(tid: Int) {
        if (!tids.add(tid)) return
        rebuild()
    }

    @Synchronized private fun rebuild() {
        val h = hint ?: return
        try {
            session?.close()
            session = h.createHintSession(tids.toIntArray(), TARGET_NS)
            if (session != null && !running) startReporter()
        } catch (e: Throwable) { lastError = "ADPF: $e"; session = null }
    }

    private fun startReporter() {
        running = true
        reporter = Thread {
            while (running) {
                try {
                    synchronized(this@Perf) { session?.reportActualWorkDuration(REPORT_NS) }
                    Thread.sleep(8)
                } catch (_: InterruptedException) { break } catch (e: Throwable) { lastError = "ADPF report: $e"; break }
            }
        }.apply { isDaemon = true; name = "perf-hint"; start() }
    }

    @Synchronized fun stop() {
        running = false
        reporter?.interrupt(); reporter = null
        try { session?.close() } catch (_: Throwable) {}
        session = null
        tids.clear()
    }

    @JvmStatic @Synchronized fun info(): String =
        "ADPF=${if (session != null) "开(${tids.size}线程)" else "关"} 唤醒锁=${if (wakeLock) "开" else "关"} 持续性能=${if (sustained) "开" else "关/不支持"}" +
            (if (lastError.isNotEmpty()) " 错误:$lastError" else "")
}
