package com.canvds.app

import android.app.Activity
import android.app.AlertDialog
import android.app.PendingIntent
import android.content.BroadcastReceiver
import android.content.ContentValues
import android.content.Context
import android.content.Intent
import android.content.IntentFilter
import android.hardware.usb.UsbConstants
import android.hardware.usb.UsbDevice
import android.hardware.usb.UsbEndpoint
import android.hardware.usb.UsbInterface
import android.hardware.usb.UsbManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.MediaStore
import android.provider.OpenableColumns
import android.view.WindowManager
import android.webkit.JsResult
import android.webkit.JavascriptInterface
import android.webkit.WebChromeClient
import android.webkit.WebView
import android.webkit.WebViewClient
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.util.function.BooleanSupplier

/**
 * Thin shell: starts the Python core (zdiag.android_main), hands it the USB connection of the CAN adapter, and shows the page the core serves.
 * All CAN / UDS / file-transfer logic lives in Python and is shared with the PC program.
 */
class MainActivity : Activity() {
    private lateinit var web: WebView
    private lateinit var usb: UsbManager
    private var device: UsbDevice? = null
    private var reader: UsbReader? = null
    private var writer: UsbWriter? = null
    private var pump: UsbPump? = null
    private var url = ""
    private var token = ""
    private val ids = listOf(0x1D50 to 0x606F, 0x1209 to 0x2323)
    private val actPerm = "com.canvds.app.USB_PERMISSION"
    private val reqPick = 41

    private val receiver = object : BroadcastReceiver() {
        override fun onReceive(c: Context, i: Intent) {
            when (i.action) {
                actPerm -> {
                    if (i.getBooleanExtra(UsbManager.EXTRA_PERMISSION_GRANTED, false)) findDevice()?.let { open(it) }
                    else usbState("没有 USB 权限，请点击“连接 USB”重试")
                }
                UsbManager.ACTION_USB_DEVICE_DETACHED -> {
                    val d: UsbDevice? = i.getParcelableExtra(UsbManager.EXTRA_DEVICE)
                    if (d != null && d.deviceName == device?.deviceName) {
                        device = null
                        py().callAttr("detach_usb")
                        usbState("适配器已拔出，请重新插入后关闭并重新打开设备")
                    }
                }
                UsbManager.ACTION_USB_DEVICE_ATTACHED -> connectUsb()
            }
        }
    }

    private fun py() = Python.getInstance().getModule("zdiag.android_main")

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        usb = getSystemService(Context.USB_SERVICE) as UsbManager
        web = WebView(this)
        setContentView(web)
        web.settings.javaScriptEnabled = true
        web.settings.domStorageEnabled = true
        web.addJavascriptInterface(Bridge(), "AndroidBridge")
        web.webViewClient = WebViewClient()
        web.webChromeClient = object : WebChromeClient() {
            override fun onJsConfirm(v: WebView, u: String, m: String, r: JsResult): Boolean {
                AlertDialog.Builder(this@MainActivity).setMessage(m)
                    .setPositiveButton("确定") { _, _ -> r.confirm() }.setNegativeButton("取消") { _, _ -> r.cancel() }
                    .setOnCancelListener { r.cancel() }.show()
                return true
            }
        }
        val f = IntentFilter().apply { addAction(actPerm); addAction(UsbManager.ACTION_USB_DEVICE_DETACHED); addAction(UsbManager.ACTION_USB_DEVICE_ATTACHED) }
        if (Build.VERSION.SDK_INT >= 33) registerReceiver(receiver, f, Context.RECEIVER_NOT_EXPORTED) else registerReceiver(receiver, f)
        Thread {
            try {
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                val info = JSONObject(py().callAttr("start", filesDir.absolutePath).toString())
                url = info.getString("url"); token = info.getString("token")
                runOnUiThread { web.loadUrl("$url/#$token") }
                connectUsb()
            } catch (e: Throwable) {
                runOnUiThread { AlertDialog.Builder(this).setTitle("启动失败").setMessage(e.toString()).setPositiveButton("确定", null).show() }
            }
        }.start()
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        if (intent.action == UsbManager.ACTION_USB_DEVICE_ATTACHED) connectUsb()
    }

    override fun onDestroy() {
        try { unregisterReceiver(receiver) } catch (_: Exception) {}
        if (isFinishing) { try { py().callAttr("stop") } catch (_: Throwable) {} }
        super.onDestroy()
    }

    @Deprecated("kept simple: no AndroidX")
    override fun onBackPressed() { if (web.canGoBack()) web.goBack() else super.onBackPressed() }

    // ------------------------------------------------------------------ USB
    private fun usbState(msg: String) = runOnUiThread {
        web.evaluateJavascript("window.onUsbState&&window.onUsbState(${JSONObject.quote(msg)})", null)
    }

    private fun findDevice(): UsbDevice? = usb.deviceList.values.firstOrNull { (it.vendorId to it.productId) in ids }

    private fun connectUsb() {
        val d = findDevice()
        if (d == null) { usbState("没有发现适配器：请用 USB 数据线连接 CANable"); return }
        if (!usb.hasPermission(d)) {
            val pi = PendingIntent.getBroadcast(this, 0, Intent(actPerm).setPackage(packageName), PendingIntent.FLAG_MUTABLE)
            usb.requestPermission(d, pi)
            return
        }
        open(d)
    }

    private fun open(d: UsbDevice) {
        try {
            val conn = usb.openDevice(d) ?: run { usbState("打开 USB 设备失败"); return }
            var intf: UsbInterface? = null
            for (i in 0 until d.interfaceCount) if (d.getInterface(i).interfaceClass == 0xFF) { intf = d.getInterface(i); break }
            intf = intf ?: d.getInterface(0)
            if (!conn.claimInterface(intf, true)) { usbState("占用 USB 接口失败"); return }
            var epIn: UsbEndpoint? = null
            var epOut: UsbEndpoint? = null
            for (i in 0 until intf.endpointCount) {
                val e = intf.getEndpoint(i)
                if (e.type != UsbConstants.USB_ENDPOINT_XFER_BULK) continue
                if (e.direction == UsbConstants.USB_DIR_IN) { if (epIn == null) epIn = e } else if (epOut == null) epOut = e
            }
            if (epIn == null || epOut == null) { usbState("没有找到批量传输端点（固件不是 Candlelight？）"); return }
            device = d
            val name = d.deviceName
            reader?.stop(); writer?.stop(); pump?.stop()
            reader = null; writer = null; pump = null
            val supplier = BooleanSupplier { usb.deviceList.containsKey(name) }
            try {
                pump = UsbPump(conn, epIn, epOut)                  // asynchronous: several transfers in flight (preferred)
                py().callAttr("attach_usb", conn, intf.id, epIn, epOut, supplier, pump, pump)
            } catch (e: Throwable) {
                pump?.stop(); pump = null
                usbState("异步 USB 不可用（$e），改用同步方式")
                reader = UsbReader(conn, epIn)
                writer = UsbWriter(conn, epOut)
                py().callAttr("attach_usb", conn, intf.id, epIn, epOut, supplier, reader, writer)
            }
            usbState("已连接 ${d.productName ?: "CAN 适配器"}（${"%04X".format(d.vendorId)}:${"%04X".format(d.productId)}），可以在下面打开设备")
        } catch (e: Throwable) {
            usbState("连接失败: $e")
        }
    }

    // ------------------------------------------------------------------ files
    override fun onActivityResult(requestCode: Int, resultCode: Int, data: Intent?) {
        super.onActivityResult(requestCode, resultCode, data)
        if (requestCode != reqPick || resultCode != RESULT_OK || data == null) return
        val uris = ArrayList<Uri>()
        data.clipData?.let { for (i in 0 until it.itemCount) uris.add(it.getItemAt(i).uri) } ?: data.data?.let { uris.add(it) }
        Thread {
            val out = JSONArray()
            val dir = File(filesDir, "outbox").apply { mkdirs() }
            for (u in uris) {
                try {
                    var name = "file"
                    contentResolver.query(u, null, null, null, null)?.use { c ->
                        val k = c.getColumnIndex(OpenableColumns.DISPLAY_NAME)
                        if (c.moveToFirst() && k >= 0) name = c.getString(k)
                    }
                    val dst = File(dir, name.replace('/', '_'))
                    contentResolver.openInputStream(u)?.use { i -> dst.outputStream().use { o -> i.copyTo(o, 256 * 1024) } }
                    out.put(dst.absolutePath)
                } catch (e: Exception) { usbState("读取文件失败: $e") }
            }
            runOnUiThread { web.evaluateJavascript("window.onFilesPicked(${JSONObject.quote(out.toString())})", null) }
        }.start()
    }

    inner class Bridge {
        @JavascriptInterface fun pickFiles() {
            runOnUiThread {
                val i = Intent(Intent.ACTION_OPEN_DOCUMENT).apply { addCategory(Intent.CATEGORY_OPENABLE); type = "*/*"; putExtra(Intent.EXTRA_ALLOW_MULTIPLE, true) }
                startActivityForResult(i, reqPick)
            }
        }
        @JavascriptInterface fun connectUsb() { Thread { this@MainActivity.connectUsb() }.start() }
        @JavascriptInterface fun defaultDest(): String = File(filesDir, "inbox").apply { mkdirs() }.absolutePath
        @JavascriptInterface fun listInbox(): String {
            val a = JSONArray()
            File(filesDir, "inbox").walkTopDown().filter { it.isFile && !it.name.endsWith(".cftpart") && !it.name.endsWith(".cftmeta") }.forEach {
                a.put(JSONObject().put("name", it.relativeTo(File(filesDir, "inbox")).path).put("size", it.length()).put("path", it.absolutePath))
            }
            return a.toString()
        }
        @JavascriptInterface fun exportFile(path: String): String {
            return try {
                val src = File(path)
                val v = ContentValues().apply {
                    put(MediaStore.Downloads.DISPLAY_NAME, src.name)
                    put(MediaStore.Downloads.RELATIVE_PATH, "Download/CanVDS")
                }
                val u = contentResolver.insert(MediaStore.Downloads.EXTERNAL_CONTENT_URI, v) ?: return "导出失败"
                contentResolver.openOutputStream(u)?.use { o -> src.inputStream().use { it.copyTo(o, 256 * 1024) } }
                "已导出到 下载/CanVDS/${src.name}"
            } catch (e: Exception) { "导出失败: $e" }
        }
    }
}
