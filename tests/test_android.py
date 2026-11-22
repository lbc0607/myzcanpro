import json, os, shutil, sys, tempfile, time, unittest, urllib.request, urllib.error
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_gsusb import FakeUsb
from zdiag import android_usb as AU, android_main as AM, gsusb as G
from zdiag.bus import CanFrame
from zdiag.busstats import ChannelCfg
from zdiag.devmgr import ConnError, DeviceManager
from zdiag.project import GS_DEVICE


class FakeConn:
    """android.hardware.usb.UsbDeviceConnection on top of the FakeUsb device."""
    def __init__(self, dev):
        self.dev, self.timeouts = dev, 0

    def controlTransfer(self, rt, req, value, index, buf, length, timeout):
        if rt == 0x41:
            self.dev.ctrl_out(req, value, bytes(buf[:length]))
            return length
        d = self.dev.ctrl_in(req, value, length)
        buf[:len(d)] = d
        return len(d)

    def bulkTransfer(self, ep, buf, length, timeout):
        if ep == "IN":
            d = self.dev.read(length, timeout)
            if d is None:
                return -1
            buf[:len(d)] = d
            return len(d)
        return len(buf) if self.dev.write(bytes(buf), timeout) else -1


class FakeReader:
    """UsbReader.kt: queue of IN transfers, drained in batches as [u16 len][bytes]..."""
    def __init__(self, dev):
        self.dev, self.stopped = dev, False

    def drain(self, timeout_ms):
        first = self.dev.read(512, timeout_ms)
        if first is None:
            return None
        parts = [first]
        while len(parts) < 256:
            c = self.dev.read(512, 0)
            if c is None:
                break
            parts.append(c)
        return b"".join(len(p).to_bytes(2, "little") + p for p in parts)

    def stop(self):
        self.stopped = True


class AndroidUsb(unittest.TestCase):
    def tearDown(self):
        AU.detach()

    def test_gsusb_device_runs_on_android_io(self):
        io = AU.AndroidUsbIO(FakeConn(FakeUsb()), 0, "IN", "OUT")
        dev = G.GsUsbDevice("gs", 0, "", io=io)
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=5000))
        bus.send(CanFrame(0x7E0, b"\x02\x10\x03"))
        for _ in range(100):
            if bus.stats.tx:
                break
            time.sleep(0.01)
        self.assertEqual(bus.stats.tx, 1)
        dev.close()

    def test_reader_batches_frames_and_close_keeps_connection(self):
        fake = FakeUsb()
        rd = FakeReader(fake)
        AU.attach(FakeConn(fake), 0, "IN", "OUT", None, rd)
        dm = DeviceManager("")
        for _ in range(2):                                                       # open, close, open again without re-plugging
            dm.open(GS_DEVICE, 0)
            dm.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000))
            bus = dm.chans[0].bus
            for k in range(50):
                fake.inject(0x100 + k, b"\x01\x02")
            got = [bus.recv(1.0) for _ in range(50)]
            self.assertTrue(all(g is not None for g in got))
            self.assertEqual(bus.send_many([CanFrame(1, b"x")] * 5), 5)
            dm.close()
            self.assertFalse(rd.stopped)
        AU.detach()
        self.assertTrue(rd.stopped)

    def test_unplug_is_reported(self):
        gone = [False]
        io = AU.AndroidUsbIO(FakeConn(FakeUsb()), 0, "IN", "OUT", present=lambda: not gone[0])
        self.assertIsNone(io.read(64, 5))
        gone[0] = True
        io._chk = 0
        self.assertRaises(G.GsUsbError, io.read, 64, 5)

    def test_device_manager_uses_attached_connection(self):
        dm = DeviceManager("")
        AU.attach(FakeConn(FakeUsb()), 0, "IN", "OUT")
        dm.open(GS_DEVICE, 0)
        self.assertEqual(dm.channel_total(), 1)
        dm.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000))
        bus = dm.chans[0].bus
        self.assertEqual(bus.send_many([CanFrame(0x100, b"1234", fd=True)] * 3), 3)
        dm.close()

    def test_without_attach_on_android_says_so(self):
        os.environ["ANDROID_ROOT"], os.environ["ANDROID_DATA"] = "/system", "/data"
        try:
            dm = DeviceManager("")
            with self.assertRaises(ConnError) as e:
                dm.open(GS_DEVICE, 0)
            self.assertIn("USB", str(e.exception))
        finally:
            del os.environ["ANDROID_ROOT"], os.environ["ANDROID_DATA"]


class WebUi(unittest.TestCase):
    def test_start_serves_page_and_calls(self):
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        info = json.loads(AM.start(home))
        self.addCleanup(AM.stop)
        self.assertEqual(json.loads(AM.start(home)), info)                       # idempotent
        page = urllib.request.urlopen(info["url"] + "/").read().decode()
        self.assertIn("CanVDS", page)
        self.assertNotIn(info["token"], page)                                    # the page carries no secret

        def post(origin, token):
            h = {"Content-Type": "application/json", "Authorization": "Bearer " + token}
            if origin:
                h["Origin"] = origin
            req = urllib.request.Request(info["url"] + "/call", json.dumps({"method": "status", "params": {}}).encode(), h, method="POST")
            try:
                return urllib.request.urlopen(req).status
            except urllib.error.HTTPError as e:
                return e.code
        self.assertEqual(post(info["url"], info["token"]), 200)                 # our own page
        self.assertEqual(post("http://evil.example", info["token"]), 403)       # any other web page
        self.assertEqual(post(info["url"], "wrong"), 401)
        self.assertEqual(post(None, info["token"]), 200)


if __name__ == "__main__":
    unittest.main()
