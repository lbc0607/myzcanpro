import os, struct, sys, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag import gsusb as G
from zdiag.bus import CanFrame
from zdiag.busstats import ChannelCfg
from zdiag.monitor import FrameTap

NOM = dict(feature=G.F_FD | G.F_HW_TIMESTAMP | G.F_BT_CONST_EXT | G.F_TERMINATION | G.F_BERR_REPORTING, fclk=160_000_000,
           tseg1_min=2, tseg1_max=256, tseg2_min=2, tseg2_max=128, sjw_max=128, brp_min=1, brp_max=512, brp_inc=1)
DAT = dict(NOM, tseg1_max=32, tseg2_max=16, sjw_max=16, brp_max=32)


def bt_bytes(d):
    return struct.pack("<10I", d["feature"], d["fclk"], d["tseg1_min"], d["tseg1_max"], d["tseg2_min"], d["tseg2_max"], d["sjw_max"],
                       d["brp_min"], d["brp_max"], d["brp_inc"])


class FakeUsb(G.UsbIO):
    """A gs_usb device on a table: records control requests, echoes every written frame, can be wired to a peer (its TX = peer's RX)."""

    def __init__(self, ts=True, fifo=64, feature=None):
        self.nom = dict(NOM, feature=NOM["feature"] if feature is None else feature)
        self.ts = ts
        self.ctrl = []
        self.rx = []                          # chunks waiting for read()
        self.cv = threading.Condition()
        self.peer = None
        self.fifo, self.held = fifo, 0        # `held`: frames accepted but not yet "on the bus" (echo withheld while paused)
        self.paused = False
        self.writes = []
        self.fd_mode = False
        self.t0 = time.perf_counter()

    def ctrl_out(self, breq, value, data):
        self.ctrl.append((breq, value, bytes(data)))
        if breq == G.BREQ_MODE:
            mode, flags = struct.unpack("<II", data)
            if mode == G.MODE_START:
                self.fd_mode = bool(flags & G.F_FD)

    def ctrl_in(self, breq, value, length):
        if breq == G.BREQ_DEVICE_CONFIG:
            return bytes((0, 0, 0, 0)) + struct.pack("<II", 7, 1)
        if breq == G.BREQ_BT_CONST:
            return bt_bytes(self.nom)
        if breq == G.BREQ_BT_CONST_EXT:
            return bt_bytes(self.nom) + bt_bytes(DAT)
        raise AssertionError(breq)

    def _push(self, b):
        with self.cv:
            self.rx.append(b)
            self.cv.notify_all()

    def read(self, size, timeout_ms):
        with self.cv:
            if not self.rx:
                self.cv.wait(timeout_ms / 1000.0)
            return self.rx.pop(0) if self.rx else None

    def now_us(self):
        return int((time.perf_counter() - self.t0) * 1e6) & 0xFFFFFFFF

    def write(self, data, timeout_ms):
        size = G.frame_size(self.fd_mode, self.ts)
        assert len(data) % size == 0, (len(data), size)
        if self.paused:
            return False
        self.writes.append(len(data) // size)
        for i in range(0, len(data), size):
            f = bytearray(data[i:i + size])
            if self.ts:
                f[-4:] = struct.pack("<I", self.now_us())
            self._push(bytes(f))                                  # echo (echo_id unchanged)
            if self.peer is not None:                             # the same frame as received by the peer
                g = bytearray(f)
                g[0:4] = struct.pack("<I", G.ECHO_RX)
                self.peer._push(bytes(g))
        return True

    def inject(self, can_id, data, fd=False, ext=False, err=False):
        cid = can_id | (G.ID_EFF if ext else 0) | (G.ID_ERR if err else 0)
        dlc = G.dlc_code(len(data)) if fd else len(data)
        n = G.frame_size(self.fd_mode, self.ts)
        b = struct.pack("<IIBBBB", G.ECHO_RX, cid, dlc, 0, G.FLAG_FD if fd else 0, 0) + bytes(data).ljust(64 if self.fd_mode else 8, b"\0")
        self._push(b + (struct.pack("<I", self.now_us()) if self.ts else b""))


def open_dev(io=None):
    io = io or FakeUsb()
    return G.GsUsbDevice("gs", 0, "", io=io), io


class Protocol(unittest.TestCase):
    def test_bittiming_exact_rates(self):
        for rate in (1_000_000, 500_000, 250_000, 125_000, 100_000, 50_000, 20_000, 10_000, 5_000):
            b = G.calc_bittiming(160_000_000, rate, G.auto_sample_pct(rate), NOM)
            tq = 1 + b["prop_seg"] + b["phase_seg1"] + b["phase_seg2"]
            self.assertEqual(b["brp"] * tq * rate, 160_000_000, rate)
            self.assertLess(abs(b["sample_pct"] - G.auto_sample_pct(rate)), 2.5, rate)
        for rate in (8_000_000, 5_000_000, 4_000_000, 2_000_000, 1_000_000):
            b = G.calc_bittiming(160_000_000, rate, G.auto_sample_pct(rate), DAT)
            tq = 1 + b["prop_seg"] + b["phase_seg1"] + b["phase_seg2"]
            self.assertEqual(b["brp"] * tq * rate, 160_000_000, rate)
            self.assertLessEqual(b["phase_seg1"] + b["prop_seg"], 32)

    def test_sample_point_followed(self):
        b = G.calc_bittiming(160_000_000, 500_000, 80.0, NOM)
        self.assertAlmostEqual(b["sample_pct"], 80.0, delta=0.5)
        self.assertRaises(ValueError, G.calc_bittiming, 160_000_000, 7_777_777, 75.0, DAT)

    def test_frame_roundtrip_classic_and_fd(self):
        for fd_mode in (False, True):
            for ts in (False, True):
                fr = CanFrame(0x123, bytes(range(5)))
                raw = G.pack_frame(fr, 9, 0, fd_mode, ts)
                self.assertEqual(len(raw), G.frame_size(fd_mode, ts))
                f = G.unpack_frames(raw, fd_mode, ts)[0]
                self.assertEqual((f["echo"], f["id"], f["data"], f["fd"]), (9, 0x123, bytes(range(5)), False))
        fr = CanFrame(0x18DAF110, bytes(range(20)), extended=True, fd=True, brs=True)
        f = G.unpack_frames(G.pack_frame(fr, 1, 0, True, True), True, True)[0]
        self.assertEqual((f["id"], f["eff"], f["fd"], f["brs"], len(f["data"])), (0x18DAF110, True, True, True, 20))

    def test_two_frames_in_one_transfer_and_short_classic(self):
        a = G.pack_frame(CanFrame(1, b"\1"), 0, 0, True, True)
        self.assertEqual(len(G.unpack_frames(a + a, True, True)), 2)
        short = G.pack_frame(CanFrame(1, b"\1\2"), 0, 0, False, False)       # 20 bytes while we expect 80
        self.assertEqual(G.unpack_frames(short, True, True)[0]["data"], b"\1\2")

    def test_header_bytes_match_linux_layout(self):
        raw = G.pack_frame(CanFrame(0x7E0, b"\x02\x10\x03"), 5, 0, False, False)
        self.assertEqual(raw[:12].hex(), "05000000" "e0070000" "03" "00" "00" "00")


class DeviceAndBus(unittest.TestCase):
    def test_open_and_start_sends_expected_control_requests(self):
        dev, io = open_dev()
        self.assertEqual(dev.channels, 1)
        self.assertEqual(io.ctrl[0][0], G.BREQ_HOST_FORMAT)
        self.assertEqual(io.ctrl[0][2], struct.pack("<I", 0xBEEF))
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=5000, terminator=True), None)
        reqs = [c[0] for c in io.ctrl]
        self.assertIn(G.BREQ_BITTIMING, reqs); self.assertIn(G.BREQ_DATA_BITTIMING, reqs); self.assertIn(G.BREQ_SET_TERMINATION, reqs)
        mode = [c for c in io.ctrl if c[0] == G.BREQ_MODE][-1]
        self.assertEqual(struct.unpack("<II", mode[2])[0], G.MODE_START)
        self.assertTrue(struct.unpack("<II", mode[2])[1] & G.F_FD)
        dev.close()
        self.assertEqual(struct.unpack("<II", [c for c in io.ctrl if c[0] == G.BREQ_MODE][-1][2])[0], G.MODE_RESET)

    def test_fd_on_classic_only_firmware_rejected(self):
        dev, _ = open_dev(FakeUsb(feature=0))
        self.assertRaises(G.GsUsbError, dev.start_channel, 0, ChannelCfg(protocol="CAN FD"))
        dev.close()

    def test_send_echo_and_receive(self):
        dev, io = open_dev()
        tap = FrameTap()
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000), tap)
        bus.send(CanFrame(0x600, bytes(range(40)), fd=True, brs=True))
        io.inject(0x700, b"\x01\x02")
        io.inject(0x701, bytes(range(32)), fd=True)
        got = [bus.recv(1.0), bus.recv(1.0)]
        self.assertEqual((got[0].can_id, got[0].data), (0x700, b"\x01\x02"))
        self.assertEqual((got[1].can_id, got[1].fd, len(got[1].data)), (0x701, True, 32))
        for _ in range(100):
            if bus.stats.tx:
                break
            time.sleep(0.01)
        self.assertEqual((bus.stats.tx, bus.stats.rx), (1, 2))
        self.assertEqual(len(io.writes), 1)
        self.assertIsNone(bus.recv(0.05))                                       # our own echo is not an RX frame
        dev.close()

    def test_flow_control_window_and_resume(self):
        io = FakeUsb()
        io.paused = False
        dev, _ = open_dev(io)
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN"), None)
        old = G.TX_WINDOW
        # no echoes: a withheld device (writes accepted, echo never) fills the window
        io.write_orig = io.write
        def silent(data, t):
            io.writes.append(1); return True
        io.write = silent
        frames = [CanFrame(0x100, b"\0") for _ in range(old + 10)]
        self.assertEqual(bus.send_many(frames), old)
        self.assertEqual(bus.send_many(frames), 0)                              # full
        self.assertRaises(G.GsUsbError, bus.send, frames[0])
        # echoes free the slots again
        for eid in list(bus._pending):
            io._push(G.pack_frame(CanFrame(0x100, b"\0"), eid, 0, False, True))
        for _ in range(100):
            if len(bus._free) == old:
                break
            time.sleep(0.01)
        self.assertEqual(len(bus._free), old)
        io.write = io.write_orig
        self.assertEqual(bus.send_many(frames[:5]), 5)
        dev.close()

    def test_usb_fifo_full_returns_prefix_zero_and_releases_slots(self):
        dev, io = open_dev()
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN"), None)
        io.paused = True
        self.assertEqual(bus.send_many([CanFrame(1, b"a")] * 3), 0)
        self.assertEqual(len(bus._free), G.TX_WINDOW)
        io.paused = False
        self.assertEqual(bus.send_many([CanFrame(1, b"a")] * 3), 3)
        dev.close()

    def test_listen_only_cannot_send(self):
        dev, _ = open_dev()
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN", listen_only=True), None)
        self.assertRaises(G.GsUsbError, bus.send, CanFrame(1, b"a"))
        dev.close()

    def test_error_frame_busoff_restarts_channel(self):
        dev, io = open_dev()
        bus = dev.start_channel(0, ChannelCfg(protocol="CAN"), FrameTap())
        n0 = len([c for c in io.ctrl if c[0] == G.BREQ_MODE])
        io.inject(G.ERR_BUSOFF, bytes(8), err=True)
        for _ in range(100):
            if bus.recoveries:
                break
            time.sleep(0.01)
        self.assertEqual(bus.recoveries, 1)
        self.assertEqual(len([c for c in io.ctrl if c[0] == G.BREQ_MODE]), n0 + 2)       # reset + start
        self.assertEqual(bus.stats.err, 1)
        dev.close()

    def test_pack_option_concatenates_frames(self):
        old = G.TX_PACK
        G.TX_PACK = 4
        try:
            dev, io = open_dev()
            bus = dev.start_channel(0, ChannelCfg(protocol="CAN FD"), None)
            self.assertEqual(bus.send_many([CanFrame(1, b"x", fd=True)] * 10), 10)
            self.assertEqual(io.writes, [4, 4, 2])
            dev.close()
        finally:
            G.TX_PACK = old

    def test_two_devices_wired_run_a_cft_transfer(self):
        import shutil, tempfile
        from zdiag.cft import CftConfig, Link, Receiver, Sender
        a, b = FakeUsb(), FakeUsb()
        a.peer, b.peer = b, a
        da, db = open_dev(a)[0], open_dev(b)[0]
        cfg = ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000)
        ba, bb = da.start_channel(0, cfg), db.start_channel(0, cfg)
        c = CftConfig(chunk_bytes=1000)
        d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, d, True)
        src, dst = os.path.join(d, "s"), os.path.join(d, "r")
        os.makedirs(src); os.makedirs(dst)
        payload = os.urandom(50_000)
        with open(os.path.join(src, "f.bin"), "wb") as f:
            f.write(payload)
        snd, rcv = Sender([Link(0, ba, True, True, c, "A")], c), Receiver([Link(0, bb, True, True, c, "B")], dst, c)
        res = {}
        tr = threading.Thread(target=lambda: res.update(r=rcv.run()), daemon=True)
        ts = threading.Thread(target=lambda: res.update(s=snd.run([os.path.join(src, "f.bin")])), daemon=True)
        tr.start(); ts.start(); ts.join(60)
        self.assertFalse(ts.is_alive())
        tr.join(10)
        with open(os.path.join(dst, "f.bin"), "rb") as f:
            self.assertEqual(f.read(), payload)
        da.close(); db.close()


if __name__ == "__main__":
    unittest.main()
