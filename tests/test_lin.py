"""LIN diagnostics: checksum / PID, transport (single + multi frame), UDS over LIN on a simulated oil pump, ops methods."""
import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag import lin
from zdiag.ops import METHODS, Ops, OpsError
from zdiag_ui.appctx import AppContext


class Helpers(unittest.TestCase):
    def test_protected_id(self):
        self.assertEqual(lin.protected_id(0x3C), 0x3C)          # LIN 2.x spec table: 0x3C -> 0x3C, 0x3D -> 0x7D
        self.assertEqual(lin.protected_id(0x3D), 0x7D)
        self.assertEqual(lin.protected_id(0x00), 0x80)
        self.assertEqual(lin.protected_id(0x01), 0xC1)

    def test_checksum(self):
        self.assertEqual(lin.checksum(bytes([0x55, 0x93, 0xE5]), 0x4A), 0xE6)           # LIN spec example (enhanced, PID 0x4A)
        self.assertEqual(lin.checksum(bytes([0x4A, 0x55, 0x93, 0xE5])), 0xE6)           # same sum as classic over all bytes


class Transport(unittest.TestCase):
    def setUp(self):
        self.m = lin.SimLinMaster()

    def test_single_frame_and_padding(self):
        r = lin.request(self.m, 0x21, bytes([0x3E, 0x00]))
        self.assertTrue(r["ok"]); self.assertEqual(r["data"], bytes([0x7E, 0x00]))
        fid, data = self.m.log[0]
        self.assertEqual((fid, data), (0x3C, bytes([0x21, 0x02, 0x3E, 0x00, 0xFF, 0xFF, 0xFF, 0xFF])))

    def test_multi_frame_response(self):
        r = lin.request(self.m, 0x21, bytes([0x22, 0xF1, 0x87]))
        self.assertTrue(r["ok"]); self.assertEqual(r["data"], b"\x62\xF1\x87OP-1000-A2")

    def test_multi_frame_request_and_multi_did(self):
        r = lin.request(self.m, 0x21, bytes([0x22, 0xF1, 0x87, 0xF1, 0x89, 0xF1, 0x91]))
        self.assertTrue(r["ok"])
        self.assertEqual(r["data"], b"\x62\xF1\x87OP-1000-A2\xF1\x89SW1.04.7\xF1\x91HW-B3")

    def test_negative_timeout_and_other_nad(self):
        r = lin.request(self.m, 0x21, bytes([0x22, 0x12, 0x34]))
        self.assertFalse(r["ok"]); self.assertEqual(r["nrc"], 0x31)
        r = lin.request(self.m, 0x30, bytes([0x3E, 0x00]), p2=0.05)
        self.assertFalse(r["ok"]); self.assertEqual(r["error"], "无响应")
        r = lin.request(self.m, 0x21, bytes([0x85, 0x01]))
        self.assertEqual(r["nrc"], 0x11)

    def test_scan_nodes(self):
        m = lin.SimLinMaster([lin.SimLinSlave(0x21, lin.PUMP_DIDS), lin.SimLinSlave(0x05, {})])
        self.assertEqual(lin.scan_nodes(m, 1, 0x30, 0.01), [0x05, 0x21])

    def test_scan_pump(self):
        r = lin.scan_pump(self.m, 0x21)
        self.assertTrue(r["ok"])
        v = {i["did"]: i["value"] for i in r["items"]}
        self.assertEqual(v["F187"], "OP-1000-A2"); self.assertEqual(v["F18A"], "DEMO PUMPS Co.")
        r = lin.scan_pump(self.m, 0x21, [(0xF187, "零件号", "hex"), (0x1234, "未知", "auto")])
        self.assertEqual(r["items"][0]["value"], "4F 50 2D 31 30 30 30 2D 41 32")
        self.assertFalse(r["items"][1]["ok"]); self.assertEqual(r["items"][1]["nrc"], "31")
        r = lin.scan_pump(self.m, 0x44, session=False)
        self.assertFalse(r["ok"]); self.assertIn("没有响应", r["error"])


class OpsLin(unittest.TestCase):
    def setUp(self):
        self.ctx = AppContext(tempfile.mkdtemp())
        self.ops = Ops(self.ctx)
        self.addCleanup(lambda: (self.ops.shutdown(), self.ctx.dev.shutdown()))

    def test_methods_registered(self):
        for m in ("lin_open", "lin_close", "lin_status", "lin_request", "lin_scan_nodes", "lin_scan_pump"):
            self.assertIn(m, METHODS)

    def test_flow(self):
        with self.assertRaises(OpsError): self.ops.lin_request("21", "3E 00")           # not open
        self.assertTrue(self.ops.lin_open("sim")["lin_open"])
        self.assertIn("已打开", self.ops.lin_open("sim")["message"])                      # idempotent
        r = self.ops.lin_request("21", "22 F1 87", parse="ascii")
        self.assertTrue(r["ok"]); self.assertEqual(r["value"], "OP-1000-A2")
        self.assertEqual(self.ops.lin_scan_nodes("10", "30")["nodes"], ["21"])
        p = self.ops.lin_scan_pump("21", ["F189", {"did": "F18A", "label": "供应商", "parse": "ascii"}])
        self.assertEqual([i["value"] for i in p["items"]], ["SW1.04.7", "DEMO PUMPS Co."])
        with self.assertRaises(OpsError): self.ops.lin_scan_pump("21", ["F1"])
        with self.assertRaises(OpsError): self.ops.lin_request("GG", "3E 00")
        self.assertFalse(self.ops.lin_close()["lin_open"])

    def test_zlg_not_wired(self):
        with self.assertRaises(OpsError) as cm: self.ops.lin_open("zlg")
        self.assertIn("还没有打开周立功设备", str(cm.exception))
        self.assertFalse(self.ops.lin_status()["lin_open"])


class FakeLib:
    """Stands in for zlgcan.dll: records the LIN UDS request and answers like an ECU."""
    def __init__(self, mode="pos"):
        self.mode, self.calls, self.cfg = mode, [], None
        for n in ("ZCAN_InitLIN", "ZCAN_StartLIN", "ZCAN_ResetLIN", "ZCAN_UDS_RequestEX"):
            setattr(self, n, self._wrap(n))

    def _wrap(self, n):
        def f(*a):
            self.calls.append(n)
            return getattr(self, "_" + n)(*a)
        f.argtypes = f.restype = None
        return _Fn(f)

    def _ZCAN_InitLIN(self, dev, ch, cfg):
        self.cfg = (ch, cfg._obj.linMode, cfg._obj.chkSumMode, cfg._obj.linBaud)
        return 77

    def _ZCAN_StartLIN(self, h): return 1
    def _ZCAN_ResetLIN(self, h): return 1

    def _ZCAN_UDS_RequestEX(self, h, obj, resp, out, n):
        import ctypes as C
        req = obj._obj.data.zcanLINUdsData.req.contents
        self.req = (req.sid, req.Nad, bytes(req.data[:req.data_len]), req.session_param.p2_timeout)
        r = resp._obj
        if self.mode == "pos":
            r.status, r.type = 0, 1
            r.positive.sid, r.positive.data_len = req.sid + 0x40, 3
            out[0], out[1], out[2] = 0xF1, 0x87, 0x41
        elif self.mode == "neg":
            r.status, r.type = 0, 0
            r.negative.neg_code, r.negative.sid, r.negative.error_code = 0x7F, req.sid, 0x31
        else:
            r.status = 1
        return 1


class _Fn:
    def __init__(self, f): self.f = f; self.argtypes = None; self.restype = None
    def __call__(self, *a): return self.f(*a)


class _Dev:
    def __init__(self, lib): self.lib, self.handle = lib, 5


class ZlgLinTests(unittest.TestCase):
    def setUp(self):
        import ctypes as C
        from zdiag import zlglin
        self.z, self.C = zlglin, C
        # make ctypes.byref/pointer transparent for the fake lib
        self._byref, self._pointer = C.byref, C.pointer
        C.byref = lambda o: _Ref(o)
        C.pointer = lambda o: self._pointer(o)

    def tearDown(self):
        self.C.byref, self.C.pointer = self._byref, self._pointer

    def test_struct_sizes(self):
        z, C = self.z, self.C
        self.assertEqual(C.sizeof(z.ZCAN_LIN_INIT_CONFIG), 8)
        self.assertEqual(C.sizeof(z._SessionParam), 16)
        self.assertEqual(C.sizeof(z._TransParam), 8)

    def test_positive_negative_error(self):
        for mode, check in (("pos", lambda r: (r["ok"], r["data"]) == (True, b"\x62\xF1\x87\x41")),
                            ("neg", lambda r: (r["ok"], r["nrc"], r["data"]) == (False, 0x31, b"\x7F\x22\x31")),
                            ("err", lambda r: not r["ok"] and "超时" in r["error"])):
            lib = FakeLib(mode)
            m = self.z.ZlgLinMaster(_Dev(lib), 1, 9600, 2)
            self.assertEqual(lib.cfg, (1, 1, 2, 9600))
            r = lin.request(m, 0x21, b"\x22\xF1\x87", 0.05)
            self.assertTrue(check(r), (mode, r))
            self.assertEqual(lib.req, (0x22, 0x21, b"\xF1\x87", 200))                     # p2 clamped to >= 200 ms
            m.close(); self.assertIn("ZCAN_ResetLIN", lib.calls)
            with self.assertRaises(lin.LinError): m.uds_request(0x21, b"\x3E\x00", 0.3)


class _Ref:
    def __init__(self, o): self._obj = o


if __name__ == "__main__":
    unittest.main()
