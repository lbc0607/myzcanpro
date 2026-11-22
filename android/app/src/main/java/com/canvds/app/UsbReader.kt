package com.canvds.app

import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit

/**
 * Keeps the bulk-IN endpoint drained on a native thread.  The Python side used to read one frame per call; when Python was busy the
 * adapter's IN queue filled up, the firmware could no longer hand over the echoes of our own frames and stopped transmitting - after a few
 * file blocks nothing could be sent any more.  Now frames queue up here and Python takes them in batches (drain).
 */
class UsbReader(private val conn: UsbDeviceConnection, private val ep: UsbEndpoint) {
    private val q = ArrayBlockingQueue<ByteArray>(100_000)
    @Volatile private var running = true
    @Volatile var dropped = 0L
    private val th = Thread {
        val buf = ByteArray(512)
        while (running) {
            val n = conn.bulkTransfer(ep, buf, buf.size, 100)
            if (n > 0 && !q.offer(buf.copyOf(n))) dropped++
        }
    }.apply { isDaemon = true; name = "usb-reader"; start() }

    /** Waits up to timeoutMs for data, then returns up to ~256 frames as [u16 length][bytes]... (little endian); null if nothing came. */
    fun drain(timeoutMs: Int): ByteArray? {
        val first = q.poll(timeoutMs.toLong(), TimeUnit.MILLISECONDS) ?: return null
        val parts = ArrayList<ByteArray>(64)
        parts.add(first)
        while (parts.size < 256) parts.add(q.poll() ?: break)
        var total = 0
        for (p in parts) total += 2 + p.size
        val out = ByteArray(total)
        var i = 0
        for (p in parts) {
            out[i++] = (p.size and 0xFF).toByte(); out[i++] = (p.size shr 8).toByte()
            System.arraycopy(p, 0, out, i, p.size); i += p.size
        }
        return out
    }

    fun stop() { running = false }
}
