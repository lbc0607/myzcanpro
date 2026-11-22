package com.canvds.app

import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbRequest
import java.nio.ByteBuffer
import java.util.concurrent.ArrayBlockingQueue
import java.util.concurrent.TimeUnit
import java.util.concurrent.TimeoutException

/**
 * Asynchronous USB I/O for the CAN adapter (UsbRequest instead of one blocking bulkTransfer per frame).
 *
 * Why: with a blocking call per transfer there is always a gap between two reads (the next call is only issued after the previous one
 * returned and Java/JNI/Python handled it).  At 500 kbit/s classic CAN the adapter delivers ~4500 echoes per second; the gaps capped the
 * receive path at ~1000-1500 frames/s, the echo window ran dry and the sender fell from 20 kB/s to 4 kB/s.  Here several IN requests are
 * always queued, so the host controller keeps taking frames from the adapter, and OUT frames are queued without waiting (in order).
 *
 * One thread calls requestWait(); everything else only queues requests.
 */
class UsbPump(private val conn: UsbDeviceConnection, private val epIn: UsbEndpoint, private val epOut: UsbEndpoint) {
    private class Slot(val buf: ByteBuffer, val isIn: Boolean)

    private val inQ = ArrayBlockingQueue<ByteArray>(100_000)
    private val freeOut = ArrayBlockingQueue<UsbRequest>(OUT_DEPTH)
    private val all = ArrayList<UsbRequest>()
    @Volatile private var running = true
    @Volatile var dropped = 0L

    private val th: Thread

    init {
        for (i in 0 until IN_DEPTH) {
            val r = UsbRequest()
            if (!r.initialize(conn, epIn)) throw IllegalStateException("UsbRequest.initialize(IN) failed")
            val s = Slot(ByteBuffer.allocateDirect(IN_SIZE), true)
            r.clientData = s
            all.add(r)
            if (!r.queue(s.buf)) throw IllegalStateException("UsbRequest.queue(IN) failed")
        }
        for (i in 0 until OUT_DEPTH) {
            val r = UsbRequest()
            if (!r.initialize(conn, epOut)) throw IllegalStateException("UsbRequest.initialize(OUT) failed")
            r.clientData = Slot(ByteBuffer.allocateDirect(OUT_SIZE), false)
            all.add(r)
            freeOut.add(r)
        }
        th = Thread {
            while (running) {
                val r = try { conn.requestWait(100) } catch (_: TimeoutException) { continue } catch (_: Exception) { null }
                if (r == null) { try { Thread.sleep(2) } catch (_: InterruptedException) {}; continue }
                val s = r.clientData as? Slot ?: continue
                if (s.isIn) {
                    val n = s.buf.position()                  // advanced by the number of bytes received
                    if (n > 0) {
                        val a = ByteArray(n)
                        s.buf.flip(); s.buf.get(a)
                        if (!inQ.offer(a)) dropped++
                    }
                    s.buf.clear()
                    if (running) r.queue(s.buf)
                } else {
                    s.buf.clear()
                    freeOut.offer(r)
                }
            }
        }.apply { isDaemon = true; name = "usb-pump"; start() }
    }

    /** Waits up to timeoutMs for data, then returns up to ~256 frames as [u16 length][bytes]... (little endian); null if nothing came. */
    fun drain(timeoutMs: Int): ByteArray? {
        val first = inQ.poll(timeoutMs.toLong(), TimeUnit.MILLISECONDS) ?: return null
        val parts = ArrayList<ByteArray>(64)
        parts.add(first)
        while (parts.size < 256) parts.add(inQ.poll() ?: break)
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

    /** blob = [u16 length][frame]...  Queues the frames in order without waiting for them to complete; returns how many were queued
     *  (fewer when all OUT requests are still in flight for longer than timeoutMs, i.e. the adapter is not taking frames). */
    fun writeAll(blob: ByteArray, timeoutMs: Int): Int {
        var i = 0
        var count = 0
        while (i + 2 <= blob.size) {
            val n = (blob[i].toInt() and 0xFF) or ((blob[i + 1].toInt() and 0xFF) shl 8)
            val r = freeOut.poll(if (count == 0) timeoutMs.toLong() else 0L, TimeUnit.MILLISECONDS) ?: break
            val s = r.clientData as Slot
            s.buf.clear(); s.buf.put(blob, i + 2, n); s.buf.flip()
            if (!r.queue(s.buf)) { freeOut.offer(r); break }
            i += 2 + n
            count++
        }
        return count
    }

    fun stop() {
        running = false
        for (r in all) { try { r.cancel() } catch (_: Exception) {} }
    }

    companion object {
        const val IN_DEPTH = 8
        const val IN_SIZE = 512
        const val OUT_DEPTH = 64
        const val OUT_SIZE = 128
    }
}
