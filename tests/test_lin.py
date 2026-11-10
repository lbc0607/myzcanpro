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
        self.assertIn("zlglin", str(cm.exception))
        self.assertFalse(self.ops.lin_status()["lin_open"])


if __name__ == "__main__":
    unittest.main()
