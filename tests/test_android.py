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


class FakeWriter:
    """UsbWriter.kt: takes [u16 len][frame]... and writes them to the device; `fail_after` simulates a transfer that gets refused."""
    def __init__(self, dev, fail_after=None):
        self.dev, self.fail_after, self.calls, self.stopped = dev, fail_after, 0, False

    def writeAll(self, blob, timeout_ms):
        self.calls += 1
        i, k = 0, 0
        while i + 2 <= len(blob):
            n = int.from_bytes(blob[i:i + 2], "little")
            if self.fail_after is not None and k >= self.fail_after:
                return k
            self.dev.write(bytes(blob[i + 2:i + 2 + n]), 20)
            i += 2 + n
            k += 1
        return k

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

    def test_pipelined_writer_sends_whole_batches(self):
        fake = FakeUsb()
        w = FakeWriter(fake)
        AU.attach(FakeConn(fake), 0, "IN", "OUT", None, FakeReader(fake), w)
        dm = DeviceManager("")
        dm.open(GS_DEVICE, 0)
        dm.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000))
        bus = dm.chans[0].bus
        frames = [CanFrame(0x100, bytes(range(60)), fd=True)] * 100
        sent, t_end = 0, time.time() + 5
        while sent < 100 and time.time() < t_end:                                # like the file-transfer link: poll until the window frees up
            k = bus.send_many(frames[sent:])
            sent += k
            if not k:
                time.sleep(0.001)
        self.assertEqual(sent, 100)
        self.assertGreaterEqual(w.calls, 4)                                      # batches of up to 32 frames per call, not one call per frame
        for _ in range(200):
            if bus.stats.tx >= 100:
                break
            time.sleep(0.01)
        self.assertEqual(bus.stats.tx, 100)                                      # every echo matched its slot
        self.assertEqual(len(bus._free), G.TX_WINDOW)
        w.fail_after = 3                                                         # the 4th transfer of a batch is refused
        self.assertEqual(bus.send_many(frames[:10]), 3)
        self.assertEqual(len(bus._free) + len(bus._pending), G.TX_WINDOW)        # slots of the unsent frames were given back
        dm.close()

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


class Bench(unittest.TestCase):
    def test_usb_bench_runs_on_a_gs_usb_device(self):
        from zdiag.ops import Ops, OpsError
        from zdiag_ui.appctx import AppContext
        home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, home, True)
        ops = Ops(AppContext(home))
        self.assertRaises(OpsError, ops.usb_bench)                               # no device open
        fake = FakeUsb(loopback=True)
        AU.attach(FakeConn(fake), 0, "IN", "OUT")
        self.addCleanup(AU.detach)
        ops.ctx.dev.open(GS_DEVICE, 0)
        r = ops.usb_bench()["results"]
        self.assertEqual([x["name"] for x in r][1:], ["写线程 1 个", "写线程 2 个", "写线程 4 个"])
        self.assertTrue(all(x["received"] == x["frames"] for x in r[1:]), r)
        ops.ctx.dev.close()


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
