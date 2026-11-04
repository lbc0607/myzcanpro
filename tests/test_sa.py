import os, shutil, subprocess, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import VirtualBus
from zdiag.isotp import IsoTp
from zdiag.uds import UdsClient
from zdiag.sim_ecu import SimEcu
from zdiag.seedkey import ZlgKeyDll, SerialAwareKey, SeedKeyError, SerialState

HERE = os.path.join(os.path.dirname(__file__), "mock")
SERIAL = b"SN2024ABCD0001"


def expected_key(seed, level, serial, a, b):          # independent re-statement of the mock algorithm
    num = a if len(seed) == 2 else b                   # 2-byte seed -> line 2, else line 3
    return bytes(s ^ (serial[i % len(serial)] if serial else 0) ^ ((num + 1) & 0xFF) ^ level for i, s in enumerate(seed))


@unittest.skipUnless(shutil.which("g++") and sys.platform.startswith("linux"), "needs g++ on Linux (mock .so)")
class SerialFileTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.mkdtemp()
        cls.so = os.path.join(cls.tmp, "serial_key.so")
        cls.demo = os.path.join(cls.tmp, "sa_demo.so")
        subprocess.check_call(["g++", "-shared", "-fPIC", "-o", cls.so, os.path.join(HERE, "serial_key.cpp")])
        subprocess.check_call(["g++", "-shared", "-fPIC", "-o", cls.demo, os.path.join(HERE, "sa.cpp")])

    @classmethod
    def tearDownClass(cls): shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_real_demo_source_marshalling(self):
        k = ZlgKeyDll(self.demo, workdir=self.tmp, reload=False)(bytes([1, 2, 0xFF, 0x10]), 1)
        self.assertEqual(k, bytes([2, 3, 0x00, 0x11]))            # demo algorithm: seed[i] + 1

    def run_case(self, req, rsp, ab):
        wd = tempfile.mkdtemp(dir=self.tmp)
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, req_id=req, rsp_id=rsp, serial=SERIAL,
                     key_fn=lambda seed, lvl: expected_key(seed, lvl, SERIAL, *ab))
        try:
            u = UdsClient(IsoTp(a, tx_id=req, rx_id=rsp))
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False), state=SerialState())
            u.session(0x03)
            self.assertTrue(u.security_access(sak, 0x01))
            u.stop_keepalive()
            self.assertTrue(ecu.unlocked)
            with open(os.path.join(wd, "EcuSerialNum.txt"), "rb") as f:
                self.assertEqual(f.read(), SERIAL + b"\n" + str(ab[0]).encode() + b"\n" + str(ab[1]).encode() + b"\n")
        finally:
            ecu.stop()

    def test_7e2(self): self.run_case(0x7E2, 0x7EA, (0, 0))
    def test_7e3(self): self.run_case(0x7E3, 0x7EB, (1, 2))
    def test_785_and_645(self):
        self.run_case(0x785, 0x78D, (2, 4)); self.run_case(0x645, 0x64D, (1, 3))

    def test_unknown_id_refuses(self):
        a, b = VirtualBus.pair(); ecu = SimEcu(b, req_id=0x700, rsp_id=0x708, serial=SERIAL)
        try:
            u = UdsClient(IsoTp(a, tx_id=0x700, rx_id=0x708)); u.session(0x03)
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=self.tmp, reload=False), state=SerialState())
            with self.assertRaises(SeedKeyError): u.security_access(sak, 0x01)
            self.assertEqual(ecu.bad_keys, 0)                      # no key was ever sent
            u.stop_keepalive()
        finally: ecu.stop()

    def test_stale_file_is_replaced(self):                          # second ECU must not see first serial
        wd = tempfile.mkdtemp(dir=self.tmp)
        open(os.path.join(wd, "EcuSerialNum.txt"), "w").write("OLDSERIAL\n9\n9\n")
        self.run_case_with(wd)
    def run_case_with(self, wd):
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, req_id=0x785, rsp_id=0x78D, serial=SERIAL, key_fn=lambda s, l: expected_key(s, l, SERIAL, 2, 4))
        try:
            u = UdsClient(IsoTp(a, 0x785, 0x78D)); u.session(0x03)
            u.security_access(SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False), state=SerialState()), 1)
            u.stop_keepalive(); self.assertTrue(ecu.unlocked)
        finally: ecu.stop()

    def test_serial_padding_stripped(self):
        sak = SerialAwareKey(None)
        self.assertEqual(sak._format_serial(b"AB12\xff\xff\x00"), "AB12")
        self.assertEqual(sak.lines_for(0x7E3, "AB12"), "AB12\n1\n2\n")
        self.assertEqual(SerialAwareKey(None, serial_format="hex")._format_serial(b"\x01\xAB"), "01AB")

    def test_flash_uses_serial_aware_key(self):
        from zdiag.flash import Flasher, FlashConfig, Segment
        wd = tempfile.mkdtemp(dir=self.tmp)
        a, b = VirtualBus.pair()
        ecu = SimEcu(b, req_id=0x785, rsp_id=0x78D, serial=SERIAL, key_fn=lambda s, l: expected_key(s, l, SERIAL, 2, 4))
        try:
            u = UdsClient(IsoTp(a, 0x785, 0x78D))
            sak = SerialAwareKey(ZlgKeyDll(self.so, workdir=wd, reload=False), state=SerialState())
            Flasher(u, sak, FlashConfig(boot_delay=0.0)).run([Segment(0x1000, bytes(range(200)))])
            self.assertEqual(ecu.reset_count, 1)
        finally: ecu.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
