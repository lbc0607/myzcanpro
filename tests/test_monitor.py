import os, sys, tempfile, threading, time, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from zdiag.bus import CanFrame
from zdiag.busstats import ChannelCfg
from zdiag.devmgr import DeviceManager
from zdiag.monitor import FrameTap, RX, TX
from zdiag.project import Addressing, SIM_DEVICE


class TapLogic(unittest.TestCase):
    def test_overwrite_by_id_counts_and_deltas(self):
        t = FrameTap()
        t.feed(0, CanFrame(0x100, b"\x01"), RX, 10.000)
        t.feed(0, CanFrame(0x200, b"\x02"), RX, 10.004)
        t.feed(0, CanFrame(0x100, b"\x03"), RX, 10.010)
        t.feed(0, CanFrame(0x100, b"\x09"), TX, 10.011)             # same id, other direction -> own row
        rows = {(r.can_id, r.dir): r for r in t.snapshot()}
        self.assertEqual(len(rows), 3)
        r = rows[(0x100, RX)]
        self.assertEqual((r.count, r.data), (2, b"\x03"))
        self.assertAlmostEqual(r.dt_id, 0.010); self.assertAlmostEqual(r.dt_prev, 0.006)
        self.assertEqual([x.seq for x in t.snapshot()], [1, 2, 3])  # first-seen order is stable
        self.assertEqual(len(t.trace()), 4); self.assertEqual(t.total, 4)

    def test_error_frames_rows_frequency_and_record(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "e.csv")
        t = FrameTap(); t.start_record(p)
        t.feed(0, CanFrame(0x100, b"\x01"), RX, 1.000)
        t.feed_err(0, 1.002); t.feed_err(0, 1.007); t.feed_err(1, 1.008)
        t.stop_record()
        rows = t.snapshot(); errs = [r for r in rows if r.err]
        self.assertEqual([(r.ch, r.count, r.id_text, r.data_text, r.type_text) for r in errs],
                         [(0, 2, "错误帧", "xxx", "-"), (1, 1, "错误帧", "xxx", "-")])
        self.assertAlmostEqual(errs[0].dt_id, 0.005)                      # interval between error frames = frequency
        self.assertEqual(t.errors, 3)
        self.assertEqual(sum(1 for r in t.trace() if r.err), 4 - 1)
        with open(p, encoding="utf-8-sig") as f:
            self.assertIn("错误帧,-,接收,,xxx", f.read())

    def test_channels_and_extended_are_separate_keys_and_reset(self):
        t = FrameTap()
        t.feed(0, CanFrame(0x1, b""), RX, 1.0); t.feed(1, CanFrame(0x1, b""), RX, 1.1)
        t.feed(0, CanFrame(0x1, b"", extended=True), RX, 1.2)
        self.assertEqual(len(t.snapshot()), 3)
        t.reset(); self.assertEqual((t.snapshot(), t.total), ([], 0))

    def test_record_csv_and_snapshot(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "r.csv"); q = os.path.join(d, "s.csv")
        t = FrameTap(); t.start_record(p)
        for i in range(50):
            t.feed(0, CanFrame(0x7E2, bytes([i, 0xAA]), fd=(i == 49)), TX if i % 2 else RX, 5.0 + i * 0.001)
        rec = t.stop_record()
        self.assertEqual((rec.written, rec.dropped), (50, 0))
        with open(p, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        self.assertEqual(len(lines), 51)
        self.assertEqual(lines[1], "1,0.000000,0,7E2,CAN,接收,2,00 AA")
        self.assertTrue(lines[-1].endswith("发送,2,31 AA") and ",CAN FD," in lines[-1])
        self.assertEqual(t.save_snapshot(q), 2)                         # TX row + RX row
        with open(q, encoding="utf-8-sig") as f:
            self.assertEqual(len(f.read().splitlines()), 3)

    def test_concurrent_producers_and_reader(self):
        t = FrameTap(); stop = threading.Event(); N, T = 5000, 4
        def prod(k):
            for i in range(N):
                t.feed(k % 2, CanFrame(0x100 + (i % 20), bytes(8)), RX if k < 2 else TX)
        th = [threading.Thread(target=prod, args=(k,)) for k in range(T)]
        def reader():
            while not stop.is_set():
                t.snapshot(); t.trace()
        rd = threading.Thread(target=reader); rd.start()
        t0 = time.perf_counter()
        [x.start() for x in th]; [x.join() for x in th]
        dt = time.perf_counter() - t0
        stop.set(); rd.join()
        self.assertEqual(t.total, N * T)
        self.assertEqual(sum(r.count for r in t.snapshot()), N * T)
        self.assertLess(dt / (N * T), 200e-6)                           # tap cost per frame stays far below a CAN frame time


class TapWithSession(unittest.TestCase):
    """Monitor and a diagnostic session run on the same channel at the same time."""

    def test_monitor_sees_uds_traffic_without_disturbing_it(self):
        from zdiag.engine import DiagSession
        from zdiag.project import DiagConfig
        dev = DeviceManager(tempfile.mkdtemp()); self.addCleanup(dev.shutdown)
        dev.open(SIM_DEVICE); cfg = DiagConfig(); cfg.settings.p2_ms = 800
        dev.start_channel(0, ChannelCfg(), cfg.addr, "new", cfg.settings.rx_padding)
        bus = dev.acquire(0, self, cfg.addr, "new", cfg.settings.rx_padding)
        sess = DiagSession(bus, cfg)
        for _ in range(20):
            rp = sess.exchange(bytes([0x22, 0xF1, 0x90])); self.assertEqual(rp.data[:3], bytes([0x62, 0xF1, 0x90]))
        time.sleep(0.2)                                                  # let the ECU chatter frames flow too
        rows = {(r.can_id, r.dir): r for r in dev.tap.snapshot()}
        req = rows[(cfg.addr.phys, TX)]; rsp = rows[(cfg.addr.resp, RX)]
        self.assertEqual(req.count, 40)                                  # 20 requests + 20 flow-control frames (multi-frame reply)
        self.assertGreaterEqual(rsp.count, 20)
        self.assertEqual(dev.stats_of(0).snapshot()[1], sum(r.count for r in rows.values() if r.dir == TX))
        self.assertEqual(req.data[0] >> 4, 3)                            # last TX on 7E2 is a flow-control frame


if __name__ == "__main__":
    unittest.main()
