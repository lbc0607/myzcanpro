"""Security access + EcuSerialNum.txt handling (platform independent: fake key DLL object)."""
import os, shutil, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import VirtualBus
from zdiag.isotp import IsoTp
from zdiag.uds import UdsClient
from zdiag.sim_ecu import SimEcu
from zdiag.seedkey import SerialAwareKey, SeedKeyError, SerialState, SERIAL_FILE

SERIAL = b"SN2024ABCD0001"


class FakeDll:
    """Stands in for ZlgKeyDll: remembers the file content seen at every call, returns the sim's xor key."""
    def __init__(self, workdir, name=SERIAL_FILE):
        self.workdir, self.name, self.seen = workdir, name, []

    def __call__(self, seed, level):
        with open(os.path.join(self.workdir, self.name), "rb") as f:
            self.seen.append(f.read())
        return bytes(b ^ 0x5A for b in seed)


class Base(unittest.TestCase):
    def setUp(self):
        self.wd = tempfile.mkdtemp()
        self.dll = FakeDll(self.wd)
        self.state = SerialState()

    def tearDown(self):
        for e in getattr(self, "ecus", []):
            e.stop()
        shutil.rmtree(self.wd, ignore_errors=True)

    def rig(self, req=0x7E3, rsp=0x7EB, serial=SERIAL, seed_len=4):
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, req_id=req, rsp_id=rsp, serial=serial, seed_len=seed_len,
                     key_fn=lambda seed, lvl: bytes(x ^ 0x5A for x in seed))
        self.ecus = getattr(self, "ecus", []) + [ecu]
        u = UdsClient(IsoTp(a, tx_id=req, rx_id=rsp))
        u.reads = 0
        def hook(kind, p):
            if kind == "TX" and p[:3] == b"\x22\xF1\x8C":
                u.reads += 1
        u.hook = hook
        return u, ecu

    def sak(self, **kw):
        kw.setdefault("state", self.state)
        return SerialAwareKey(self.dll, **kw)

    def file(self, name=SERIAL_FILE):
        with open(os.path.join(self.wd, name), "rb") as f:
            return f.read()


class SerialTests(Base):
    def test_first_unlock_reads_serial_and_writes_file(self):
        u, ecu = self.rig()
        u.session(0x03)
        self.assertTrue(u.security_access(self.sak(), 1))
        u.stop_keepalive()
        self.assertTrue(ecu.unlocked); self.assertEqual(u.reads, 1)
        self.assertEqual(self.file(), SERIAL + b"\n1\n2\n")
        self.assertEqual(self.dll.seen, [SERIAL + b"\n1\n2\n"])      # file existed when the DLL ran

    def test_second_unlock_does_not_read_again(self):
        u, ecu = self.rig(); u.session(0x03)
        k = self.sak()
        u.security_access(k, 1); ecu.unlocked = False
        u.security_access(k, 1); u.stop_keepalive()
        self.assertEqual(u.reads, 1)
        self.assertEqual(self.file(), SERIAL + b"\n1\n2\n")

    def test_other_ecu_keeps_line1_and_rewrites_numbers(self):
        u1, _ = self.rig(0x7E3, 0x7EB); u1.session(0x03)
        u2, _ = self.rig(0x785, 0x78D); u2.session(0x03)
        k = self.sak()
        u1.security_access(k, 1)
        u2.security_access(k, 1)
        u1.stop_keepalive(); u2.stop_keepalive()
        self.assertEqual((u1.reads, u2.reads), (1, 0))
        self.assertEqual(self.file(), SERIAL + b"\n2\n4\n")

    def test_negative_response_gives_empty_first_line_and_is_final(self):
        u, ecu = self.rig(serial=None); u.session(0x03)
        k = self.sak()
        u.security_access(k, 1); u.stop_keepalive()
        self.assertEqual(self.file(), b"\n1\n2\n")
        self.assertTrue(self.state.done)
        ecu.unlocked = False
        u.security_access(k, 1); u.stop_keepalive()
        self.assertEqual(u.reads, 1)

    def test_timeout_gives_empty_line_and_retries_next_time(self):
        u, ecu = self.rig(); u.session(0x03)
        u.tp.rx_id = 0x123                                          # deaf: the ECU's answer is never seen
        u.p2 = 0.05
        k = self.sak()
        with self.assertRaises(Exception):
            u.security_access(k, 1)
        self.assertEqual(self.file(), b"\n1\n2\n")
        self.assertFalse(self.state.done)
        time.sleep(1.5)                                             # let the ECU give up waiting for flow control
        u.tp.rx_id = 0x7EB; u.p2 = 0.5
        u.security_access(k, 1); u.stop_keepalive()
        self.assertEqual(self.file(), SERIAL + b"\n1\n2\n"); self.assertTrue(self.state.done)

    def test_unknown_physical_address_refused_without_traffic(self):
        u, ecu = self.rig(0x700, 0x708); u.session(0x03)
        with self.assertRaises(SeedKeyError) as e:
            u.security_access(self.sak(), 1)
        u.stop_keepalive()
        self.assertIn("700", str(e.exception))
        self.assertEqual(u.reads, 0); self.assertFalse(os.path.exists(os.path.join(self.wd, SERIAL_FILE)))
        self.assertFalse(self.state.done)

    def test_custom_map_did_file_and_hex_format(self):
        wd2 = self.wd
        self.dll = FakeDll(wd2, "Sn.txt")
        u, ecu = self.rig(0x645, 0x64D, serial=b"\x01\xAB")
        u.hook = None
        k = self.sak(id_map={0x645: (7, 9)}, serial_format="hex", file_name="Sn.txt")
        reads = []
        u.hook = lambda kind, p: reads.append(p) if kind == "TX" and p[0] == 0x22 else None
        u.session(0x03); u.security_access(k, 1); u.stop_keepalive()
        self.assertEqual(self.file("Sn.txt"), b"01AB\n7\n9\n")
        self.assertEqual(reads, [b"\x22\xF1\x8C"])

    def test_custom_serial_did(self):
        u, ecu = self.rig()
        ecu.dids[0xF190] = b"VIN123"
        reads = []
        u.hook = lambda kind, p: reads.append(p) if kind == "TX" and p[0] == 0x22 else None
        u.session(0x03); u.security_access(self.sak(serial_did=0xF190), 1); u.stop_keepalive()
        self.assertEqual(self.file(), b"VIN123\n1\n2\n"); self.assertEqual(reads, [b"\x22\xF1\x90"])

    def test_non_printable_serial_ascii_error_hex_ok(self):
        k = self.sak()
        with self.assertRaises(SeedKeyError):
            k._format_serial(b"\x01\x02")
        self.assertEqual(self.sak(serial_format="hex")._format_serial(b"\x01\x02"), "0102")

    def test_keepalive_not_starved_during_prepare(self):
        u, ecu = self.rig(); u.session(0x03)
        u.start_keepalive(period=0.05)
        class Slow(SerialAwareKey):
            def _read_serial(s, uds):
                time.sleep(0.5)                                     # slow serial read must not hold the request lock
                return super()._read_serial(uds)
        before = ecu.tester_present_count
        t0 = time.time()
        # the sleep itself holds no lock, so keepalive frames keep flowing
        u.security_access(Slow(self.dll, state=self.state), 1)
        u.stop_keepalive()
        self.assertGreaterEqual(ecu.tester_present_count - before, 5, time.time() - t0)

    def test_cleanup_and_reset_delete_file(self):
        u, _ = self.rig(); u.session(0x03)
        u.security_access(self.sak(), 1); u.stop_keepalive()
        self.assertTrue(os.path.exists(os.path.join(self.wd, SERIAL_FILE)))
        self.state.cleanup()
        self.assertFalse(os.path.exists(os.path.join(self.wd, SERIAL_FILE)))
        self.assertFalse(self.state.done)
        open(os.path.join(self.wd, SERIAL_FILE), "w").close()
        self.state.reset(self.wd, SERIAL_FILE)
        self.assertFalse(os.path.exists(os.path.join(self.wd, SERIAL_FILE)))


class SharedSettingsTests(unittest.TestCase):
    def test_persist_and_migration(self):
        from zdiag_ui.appctx import AppContext
        import json
        d = tempfile.mkdtemp()
        c = AppContext(d)
        self.assertEqual(c.security.id_map["7e3"], [1, 2])
        c.security.serial_dir, c.security.serial_file, c.security.id_map = "sub", "N.txt", {"7e2": [3, 4]}
        c.logcfg.max_file_mb, c.logcfg.keep_days = 5, 0
        c.save()
        c2 = AppContext(d)
        self.assertEqual((c2.security.serial_dir, c2.security.serial_file, c2.security.id_map), ("sub", "N.txt", {"7e2": [3, 4]}))
        self.assertEqual((c2.logcfg.max_file_mb, c2.logcfg.keep_days), (5, 0))
        # config written by the previous version (settings inside "diag")
        d2 = tempfile.mkdtemp()
        with open(os.path.join(d2, "myzcanpro_config.json"), "w", encoding="utf-8") as f:
            json.dump({"diag": {"settings": {"serial_did": "F1 90", "serial_file": "Old.txt", "p2_ms": 777},
                                "serial_id_map": {"7E2": [9, 9]}}}, f)
        c3 = AppContext(d2)
        self.assertEqual((c3.security.serial_did, c3.security.serial_file, c3.security.id_map), ("F1 90", "Old.txt", {"7e2": [9, 9]}))
        self.assertEqual(c3.diag_cfg.settings.p2_ms, 777)

    def test_sessions_share_the_context_object_and_folder(self):
        from zdiag_ui.appctx import AppContext
        from zdiag.engine import DiagSession
        c = AppContext(tempfile.mkdtemp())
        a, b = VirtualBus.pair()
        s1 = DiagSession(a, c.diag_cfg, security=c.security, app_dir=c.dir)
        s2 = DiagSession(b, c.diag_cfg, security=c.security, app_dir=c.dir)
        c.security.serial_dir = os.path.join(c.dir, "elsewhere")
        self.assertIs(s1.security, s2.security)
        self.assertEqual(s1.security.folder(s1.app_dir), os.path.join(c.dir, "elsewhere"))
        s1.close(); s2.close()

    def test_folder_resolution(self):
        from zdiag.project import SecurityCfg
        base = os.path.abspath(os.sep + "app")
        self.assertEqual(SecurityCfg().folder(base), base)
        self.assertEqual(SecurityCfg(serial_dir="x").folder(base), os.path.join(base, "x"))
        other = os.path.abspath(os.sep + "py")
        self.assertEqual(SecurityCfg(serial_dir=other).folder(base), other)       # absolute wins


if __name__ == "__main__":
    unittest.main(verbosity=2)
