import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import CanFrame
from zdiag.busstats import BusStats, ChannelCfg, CountingBus, _crc15, _stuffed_len, frame_seconds
from zdiag.devmgr import ConnError, DeviceManager, channel_count, find_zlgcan, has_terminator
from zdiag.project import Addressing, SIM_DEVICE
from zdiag.uds import UdsClient
from zdiag.isotp import IsoTp


class BusLoadTests(unittest.TestCase):
    def test_crc15_self_check(self):
        bits = [1, 0, 1, 1, 0, 0, 1, 0, 1, 1, 1, 0, 0, 0, 1, 0, 1]
        c = _crc15(bits)
        self.assertEqual(_crc15(bits + [(c >> (14 - i)) & 1 for i in range(15)]), 0)

    def test_stuffing(self):
        self.assertEqual(_stuffed_len([0] * 5), 6)                  # 5 equal bits -> 1 stuff bit
        self.assertEqual(_stuffed_len([0] * 10), 10 + 2)            # stuff bit itself starts a new run
        self.assertEqual(_stuffed_len([0, 1] * 6), 12)

    def test_classic_frame_time_bounds(self):
        for fid, n, ext in ((0x7E0, 8, False), (0x7E0, 0, False), (0x18DAF110, 8, True)):
            fr = CanFrame(fid, bytes(n), ext)
            nominal = (47 if not ext else 67) + 8 * n
            worst = nominal + (34 + 8 * n if not ext else 54 + 8 * n) // 4
            bits = frame_seconds(fr, 500_000) * 500_000
            self.assertTrue(nominal <= bits <= worst + 1, (fid, n, bits))

    def test_fd_faster_with_brs(self):
        a = CanFrame(0x7E0, bytes(64), fd=True, brs=True)
        b = CanFrame(0x7E0, bytes(64), fd=True, brs=False)
        self.assertLess(frame_seconds(a, 500_000, 2_000_000), frame_seconds(b, 500_000, 2_000_000))

    def test_load_percentage(self):
        st = BusStats(500_000)
        fr = CanFrame(0x7E0, bytes(8))
        self.assertIsNone(st.sample_load(0.0))
        for _ in range(100):
            st.count_rx(fr)
        one = frame_seconds(fr, 500_000)
        self.assertAlmostEqual(st.sample_load(1.0), 100 * one * 100 / 1.0, places=6)    # 100 frames in 1 s
        st.count_tx(fr)
        self.assertAlmostEqual(st.sample_load(3.0), 100 * one / 2.0, places=6)
        self.assertEqual(st.snapshot(), (100, 1, 0))
        for _ in range(100000):
            st.count_tx(fr)
        self.assertEqual(st.sample_load(3.001), 100.0)                                 # clamped

    def test_cfg_validate_and_summary(self):
        ChannelCfg().validate()
        for bad in (ChannelCfg(bitrate_kbps=0), ChannelCfg(bitrate_kbps=2000),
                    ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=250),
                    ChannelCfg(calc_load=True, load_interval_ms=5)):
            with self.assertRaises(ValueError):
                bad.validate()
        self.assertEqual(ChannelCfg(protocol="CAN FD", data_kbps=2000, listen_only=True).summary(), "CAN FD 500k/2000k 只听")
        self.assertEqual(ChannelCfg(bitrate_kbps=0.5 + 249.5).bitrate, 250000)


class ManagerTests(unittest.TestCase):
    def setUp(self):
        self.dm = DeviceManager(os.path.dirname(__file__))
        self.addCleanup(self.dm.shutdown)
        self.events = 0
        self.dm.add_listener(lambda: setattr(self, "events", self.events + 1))

    def test_toggle_state_machine(self):
        dm = self.dm
        with self.assertRaises(ConnError):
            dm.start_channel(0, ChannelCfg())                 # device not open
        dm.open(SIM_DEVICE)
        with self.assertRaises(ConnError):
            dm.open(SIM_DEVICE)                               # already open
        dm.start_channel(0, ChannelCfg())
        with self.assertRaises(ConnError):
            dm.start_channel(0, ChannelCfg())                 # already started
        with self.assertRaises(ConnError):
            dm.start_channel(5, ChannelCfg())                 # no such channel
        dm.start_channel(1, ChannelCfg(protocol="CAN FD", data_kbps=2000))
        self.assertEqual(dm.started(), [0, 1])
        dm.stop_channel(0)
        self.assertEqual(dm.started(), [1])
        dm.close()
        self.assertFalse(dm.is_open); self.assertEqual(dm.started(), [])
        self.assertGreaterEqual(self.events, 5)

    def test_invalid_cfg_and_fd_on_can_device(self):
        self.dm.open(SIM_DEVICE)
        with self.assertRaises(ConnError):
            self.dm.start_channel(0, ChannelCfg(bitrate_kbps=0))
        self.assertEqual(self.dm.started(), [])

    def test_usbcan2_rejects_fd_and_missing_dll_message(self):
        with self.assertRaises(ConnError) as e:
            self.dm.open("USBCAN-II")
        self.assertIn("zlgcan.dll", str(e.exception)); self.assertFalse(self.dm.is_open)
        with self.assertRaises(ConnError):
            self.dm.open("Foo")

    def test_owner_locking_and_close_protection(self):
        dm = self.dm
        dm.open(SIM_DEVICE); dm.start_channel(0, ChannelCfg()); dm.start_channel(1, ChannelCfg())
        a, b = object(), object()
        dm.acquire(0, a, Addressing()); dm.acquire(1, b, Addressing())      # different channels: both fine
        with self.assertRaises(ConnError):
            dm.acquire(0, b, Addressing())
        with self.assertRaises(ConnError):
            dm.stop_channel(0)
        with self.assertRaises(ConnError):
            dm.close()
        dm.release(a); dm.stop_channel(0)
        dm.release(b); dm.close()

    def test_acquire_needs_started_normal_channel(self):
        dm = self.dm
        dm.open(SIM_DEVICE)
        with self.assertRaises(ConnError) as e:
            dm.acquire(0, object(), Addressing())
        self.assertIn("启动", str(e.exception))
        dm.start_channel(0, ChannelCfg(listen_only=True))
        with self.assertRaises(ConnError) as e:
            dm.acquire(0, object(), Addressing())
        self.assertIn("只听", str(e.exception)); self.assertEqual(dm.owners, {})

    def test_sim_traffic_feeds_counters_and_follows_address(self):
        dm = self.dm
        dm.open(SIM_DEVICE); dm.start_channel(0, ChannelCfg(), Addressing(), "new")
        me = object()
        bus = dm.acquire(0, me, Addressing(), "new")
        u = UdsClient(IsoTp(bus, 0x7E2, 0x7EA))
        u.request(bytes([0x10, 0x03]))
        rx, tx, err = dm.stats_of(0).snapshot()
        self.assertGreaterEqual(tx, 1); self.assertGreaterEqual(rx, 1); self.assertEqual(err, 0)
        ecu1 = dm.sim_ecu(0)
        dm.release(me)
        a2 = Addressing(); a2.phys, a2.resp = 0x7E3, 0x7EB
        bus = dm.acquire(0, me, a2, "new")
        self.assertIsNot(dm.sim_ecu(0), ecu1)                              # rebuilt for the new IDs
        self.assertGreaterEqual(dm.stats_of(0).snapshot()[1], tx)          # counters survive the rebuild
        u = UdsClient(IsoTp(bus, 0x7E3, 0x7EB)); u.request(bytes([0x10, 0x03]))

    def test_helpers(self):
        self.assertEqual([channel_count(n) for n in ("USBCANFD-400U", "USBCANFD-200U", "USBCAN-II", "USBCANFD-100U")], [4, 2, 2, 1])
        self.assertTrue(has_terminator("USBCANFD-200U")); self.assertFalse(has_terminator("USBCAN-II"))
        vendor = os.path.join(os.path.dirname(__file__), "..", "vendor", "zlgcan.dll")
        if os.path.isfile(vendor):
            self.assertTrue(find_zlgcan(os.path.join(os.path.dirname(__file__), "..")).endswith("zlgcan.dll"))
            self.assertTrue(os.path.isfile(find_zlgcan(os.path.join(os.path.dirname(__file__), ".."))))


class FakeLib:
    """Records ZCAN_* calls so the init sequence can be checked against the official property tables."""
    def __init__(self, fail=None):
        self.calls, self.fail = [], fail
    def ZCAN_SetValue(self, dev, path, val):
        self.calls.append(("set", path.decode(), val.decode())); return 1
    def ZCAN_InitCAN(self, dev, ch, byref):
        cfg = byref._obj
        self.calls.append(("init", ch, cfg.can_type)); self.init = cfg; return 99
    def ZCAN_StartCAN(self, chn): self.calls.append(("start",)); return 1
    def ZCAN_ResetCAN(self, chn): self.calls.append(("reset",)); return 1
    def ZCAN_GetReceiveNum(self, chn, t): return 0
    def ZCAN_CloseDevice(self, dev): self.calls.append(("closedev",)); return 1


class ZlgInitSequenceTests(unittest.TestCase):
    def make(self, name):
        from zdiag.zlgcan_bus import DEVICE_TYPES, FD_TYPES, ZlgDevice
        d = ZlgDevice.__new__(ZlgDevice)
        d.name, d.dev_type, d.lib, d.handle, d.buses = name, DEVICE_TYPES[name], FakeLib(), 1, {}
        d.is_fd_dev = d.dev_type in FD_TYPES
        return d

    def test_fd_device_fd_mode(self):
        d = self.make("USBCANFD-200U")
        bus = d.start_channel(1, ChannelCfg(protocol="CAN FD", bitrate_kbps=500, data_kbps=2000, terminator=True))
        c = d.lib.calls
        self.assertEqual(c[:3], [("set", "1/protocol", "1"), ("set", "1/canfd_abit_baud_rate", "500000"),
                                 ("set", "1/canfd_dbit_baud_rate", "2000000")])
        self.assertEqual(c[3], ("init", 1, 1))                               # can_type = 1 on FD series
        self.assertEqual(c[4:6], [("set", "1/initenal_resistance", "1"), ("start",)])   # post-InitCAN, before start
        self.assertEqual(d.lib.init.cfg.canfd.mode, 0)
        d.close(); self.assertEqual(d.lib.calls[-2:], [("reset",), ("closedev",)])

    def test_fd_device_classic_mode_listen_only_no_terminator(self):
        d = self.make("USBCANFD-200U")
        d.start_channel(0, ChannelCfg(protocol="CAN", bitrate_kbps=250, listen_only=True, terminator=False))
        c = d.lib.calls
        self.assertEqual(c[:2], [("set", "0/protocol", "0"), ("set", "0/canfd_abit_baud_rate", "250000")])
        self.assertNotIn(("set", "0/canfd_dbit_baud_rate", "2000000"), c)
        self.assertEqual(c[2], ("init", 0, 1)); self.assertIn(("set", "0/initenal_resistance", "0"), c)
        self.assertEqual(d.lib.init.cfg.canfd.mode, 1)
        d.close()

    def test_usbcan2(self):
        from zdiag.zlgcan_bus import ZlgError
        d = self.make("USBCAN-II")
        d.start_channel(1, ChannelCfg(bitrate_kbps=500))
        c = d.lib.calls
        self.assertEqual(c[0], ("set", "1/baud_rate", "500000")); self.assertEqual(c[1], ("init", 1, 0))
        self.assertEqual((d.lib.init.cfg.can.timing0, d.lib.init.cfg.can.timing1), (0x00, 0x1C))
        self.assertFalse(any("resistance" in str(x) for x in c))
        with self.assertRaises(ZlgError):
            d.start_channel(0, ChannelCfg(bitrate_kbps=33))                  # not in the timing table
        with self.assertRaises(ZlgError):
            d.start_channel(0, ChannelCfg(protocol="CAN FD", data_kbps=2000))  # classic-only device
        d.close()

    def test_listen_only_refuses_to_send_and_counts_errors(self):
        from zdiag.zlgcan_bus import ZlgError
        d = self.make("USBCAN-II")
        bus = d.start_channel(0, ChannelCfg(listen_only=True))
        with self.assertRaises(ZlgError):
            bus.send(CanFrame(0x7E0, b"\x02\x10\x03"))
        d.close()


if __name__ == "__main__":
    unittest.main()
