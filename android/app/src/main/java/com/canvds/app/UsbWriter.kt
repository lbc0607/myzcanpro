package com.canvds.app

import android.hardware.usb.UsbDeviceConnection
import android.hardware.usb.UsbEndpoint
import java.util.concurrent.CountDownLatch
import java.util.concurrent.Executors

/**
 * Several bulk-OUT transfers in flight at once.  One synchronous bulkTransfer costs 2-6 ms on a phone (scheduling + wake-up), so a single
 * caller could not exceed ~300 frames/s; with a pool of threads the transfers overlap and the USB bus becomes the limit again.
 * Frames of one batch may reach the adapter in a slightly different order - the file-transfer protocol numbers every frame.
 */
class UsbWriter(private val conn: UsbDeviceConnection, private val ep: UsbEndpoint, threads: Int = 8) {
    private val pool = Executors.newFixedThreadPool(threads) { r -> Thread(r, "usb-writer").apply { isDaemon = true } }

    /** blob = [u16 length][frame]... (little endian). Returns how many frames, counted from the first, were accepted by the adapter. */
    fun writeAll(blob: ByteArray, timeoutMs: Int): Int {
        val frames = ArrayList<ByteArray>()
        var i = 0
        while (i + 2 <= blob.size) {
            val n = (blob[i].toInt() and 0xFF) or ((blob[i + 1].toInt() and 0xFF) shl 8)
            frames.add(blob.copyOfRange(i + 2, i + 2 + n))
            i += 2 + n
        }
        val ok = BooleanArray(frames.size)
        val latch = CountDownLatch(frames.size)
        for ((k, f) in frames.withIndex()) {
            pool.execute {
                try { ok[k] = conn.bulkTransfer(ep, f, f.size, timeoutMs) >= f.size } finally { latch.countDown() }
            }
        }
        latch.await()
        var c = 0
        while (c < ok.size && ok[c]) c++
        return c
    }

    fun stop() { pool.shutdownNow() }
}
