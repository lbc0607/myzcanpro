"""Operations layer against the simulated device (real AppContext)."""
import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.ops import METHODS, Ops, OpsError
from zdiag.project import SIM_DEVICE
from zdiag_ui.appctx import AppContext


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx = AppContext(tempfile.mkdtemp())
        self.ops = Ops(self.ctx)
        self.addCleanup(self.close)

    def close(self):
        self.ops.shutdown(); self.ctx.dev.shutdown()

    def up(self, *chans):
        self.ops.device_open(SIM_DEVICE)
        for c in (chans or (0,)):
            self.ops.channel_start(c)


class DeviceTests(Base):
    def test_open_start_stop_close_and_errors(self):
        with self.assertRaises(OpsError): self.ops.channel_start(0)                       # device not open
        r = self.ops.device_open(SIM_DEVICE); self.assertTrue(r["is_open"])
        self.assertIn("已打开", self.ops.device_open(SIM_DEVICE)["message"])               # idempotent
        r = self.ops.channel_start(0, protocol="CAN FD", bitrate_kbps=500, data_kbps=2000)
        self.assertEqual(r["channels"][0]["config"]["protocol"], "CAN FD")
        self.assertIn("已启动", self.ops.channel_start(0)["message"])
        with self.assertRaises(OpsError): self.ops.channel_start(1, protocol="LIN")
        with self.assertRaises(OpsError): self.ops.channel_start(1, bitrate_kbps=5000)
        with self.assertRaises(OpsError): self.ops.channel_start(99)
        self.assertEqual(self.ops.channel_stop(0)["channels"], [])
        self.up(0); self.assertFalse(self.ops.device_close()["is_open"])
        self.assertIn(SIM_DEVICE, self.ops.device_list()["devices"])
        with self.assertRaises(OpsError): self.ops.device_open("不存在的设备")


class DiagTests(Base):
    def test_uds_read_did_dtc_and_negative(self):
        self.up()
        r = self.ops.uds_request(0, "10 03"); self.assertTrue(r["ok"], r); self.assertTrue(r["data"].startswith("50 03"))
        self.assertTrue(any("REQ" in l for l in r["log"]))
        r = self.ops.uds_request(0, "22 F1 90", parse="ascii"); self.assertTrue(r["ok"]); self.assertEqual(r["request"], "22 F1 90")
        self.assertTrue(r["value"])
        self.assertTrue(self.ops.did_read(0, "F190")["ok"])
        r = self.ops.uds_request(0, "22 FF FF"); self.assertFalse(r["ok"]); self.assertIn("nrc", r) if "nrc" in r else self.assertIn("error", r)
        d = self.ops.dtc_read(0); self.assertTrue(d["ok"], d); self.assertEqual(d["count"], len(d["dtcs"]))
        self.assertTrue(self.ops.ecu_reset(0)["ok"]); self.assertTrue(self.ops.session_control(0, "03")["ok"])
        with self.assertRaises(OpsError): self.ops.uds_request(0, "zz")
        with self.assertRaises(OpsError): self.ops.uds_request(0, "")
        with self.assertRaises(OpsError): self.ops.did_read(0, "F1")
        with self.assertRaises(OpsError): self.ops.uds_request(0, "10 01", module="没有")
        with self.assertRaises(OpsError): self.ops.uds_request(5, "10 01")                  # channel not started
        self.assertEqual(self.ctx.dev.owners, {})                                          # nothing left held

    def test_scan_and_function(self):
        self.up()
        s = self.ops.scan(0, "动力域控制器", ["code", "dtc"]); self.assertTrue(s["ok"], s); self.assertEqual(set(s["fields"]), {"code", "dtc"})
        f = self.ops.function_run(0, "动力域控制器", "故障检测"); self.assertTrue(f["ok"], f)
        with self.assertRaises(OpsError): self.ops.function_run(0, "动力域控制器", "../x")
        with self.assertRaises(OpsError): self.ops.scan(0, "动力域控制器", ["zzz"])
        self.assertIn("动力域控制器", [m["name"] for m in self.ops.modules()["modules"]])

    def test_keepalive_holds_and_releases_channel(self):
        self.up(); self.ops.keepalive_start(0, period_ms=50)
        with self.assertRaises(OpsError): self.ops.keepalive_start(0)
        self.assertTrue(self.ops.uds_request(0, "10 01")["ok"])                              # still usable alongside
        time.sleep(0.2); self.ops.keepalive_stop(); self.assertEqual(self.ctx.dev.owners, {})


class FrameTests(Base):
    def test_frame_send_cyclic_and_monitor(self):
        self.up(); self.ops.monitor_clear()
        r = self.ops.frame_send(0, "321", "01 02 03", count=3, interval_ms=5); self.assertTrue(r["ok"]); self.assertEqual(r["sent"], 3)
        tr = self.ops.monitor_trace(10, ids="321"); self.assertEqual(len(tr["frames"]), 3); self.assertEqual(tr["frames"][0]["dir"], "TX")
        self.assertEqual(tr["frames"][0]["data"], "01 02 03")
        self.ops.cyclic_start(0, "c1", "555", "AA BB", period_ms=10)
        with self.assertRaises(OpsError): self.ops.cyclic_start(0, "c1", "555", "AA")
        cap = self.ops.monitor_capture(0.3, ids="555"); self.assertGreaterEqual(cap["returned"], 5)
        self.assertEqual(self.ops.cyclic_list()["senders"][0]["key"], "c1")
        st = self.ops.cyclic_stop("c1")["stopped"][0]; self.assertGreaterEqual(st["sent"], 5)
        with self.assertRaises(OpsError): self.ops.cyclic_stop("c1")
        self.assertEqual(self.ctx.dev.owners, {})
        self.assertEqual(self.ops.monitor_snapshot(ids="321")["ids"][0]["count"], 3)

    def test_limits_and_validation(self):
        self.up()
        for bad in (dict(can_id="zz"), dict(can_id="321", data="01 02", count=0), dict(can_id="FFF"), dict(can_id="321", fd=True),
                    dict(can_id="321", data="00" * 9), dict(can_id="321", count=100000, interval_ms=1000)):
            with self.assertRaises(OpsError, msg=str(bad)): self.ops.frame_send(0, **bad)
        with self.assertRaises(OpsError): self.ops.cyclic_start(0, "../k", "1")
        with self.assertRaises(OpsError): self.ops.cyclic_start(0, "k", "1", count=0)
        with self.assertRaises(OpsError): self.ops.monitor_capture(0)
        with self.assertRaises(OpsError): self.ops.monitor_trace(5, ids="xyz")

    def test_finite_cyclic_ends_and_releases(self):
        self.up(); self.ops.cyclic_start(0, "n", "123", "01", period_ms=5, count=4)
        time.sleep(0.4)
        l = self.ops.cyclic_list()["senders"]; self.assertEqual((l[0]["running"], l[0]["sent"]), (False, 4))
        self.assertEqual(self.ctx.dev.owners, {})

    def test_stop_channel_stops_its_senders(self):
        self.up(); self.ops.cyclic_start(0, "k", "123", "01", period_ms=5)
        self.ops.channel_stop(0); self.assertEqual(self.ops.cyclic_list()["senders"][0]["running"], False)

    def test_recording_writes_file(self):
        self.up(); r = self.ops.record_start("t1"); self.ops.frame_send(0, "100", "01"); time.sleep(0.3); self.ops.record_stop()
        self.assertTrue(os.path.isfile(r["path"]))
        with open(r["path"], encoding="utf-8-sig") as f: self.assertIn("100", f.read())
        with self.assertRaises(OpsError): self.ops.record_start("../x")


class SettingsTests(Base):
    def test_get_set_validate_and_persist(self):
        g = self.ops.settings_get(); self.assertEqual(set(g), {"diag", "security", "log", "channel"})
        r = self.ops.settings_set("diag", {"addr": {"phys": "7E3", "resp": "7EB"}, "settings": {"p2_ms": 500}})
        self.assertEqual(self.ctx.diag_cfg.addr.phys, 0x7E3); self.assertEqual(self.ctx.diag_cfg.settings.p2_ms, 500)
        self.assertEqual(r["diag"]["addr"]["resp"], "7EB")
        ctx2 = AppContext(self.ctx.dir); self.assertEqual(ctx2.diag_cfg.settings.p2_ms, 500); ctx2.dev.shutdown()           # persisted
        for sec, vals in (("diag", {"settings": {"p2_ms": 0}}), ("diag", {"settings": {"nope": 1}}), ("diag", {"settings": {"p2_ms": "x"}}),
                          ("diag", {"addr": {"phys": "zz"}}), ("diag", {"addr": {"phys": "FFF"}}), ("diag", {"keepalive": {"addr": "x"}}),
                          ("diag", {"bogus": {}}), ("log", {"max_file_mb": 0}), ("log", {"keep_days": -1}), ("security", {"serial_format": "x"}),
                          ("security", {"id_map": {"7e2": "bad"}}), ("channel", {"sim_profile": "x"}), ("nope", {"a": 1}), ("diag", {})):
            with self.assertRaises(OpsError, msg=str((sec, vals))): self.ops.settings_set(sec, vals)
        self.assertEqual(self.ctx.diag_cfg.settings.p2_ms, 500)                             # failed calls changed nothing
        self.ops.settings_set("security", {"serial_file": "S.txt", "id_map": {"7E2": [0, 0], "785": [2, 4]}})
        self.assertEqual(self.ctx.security.serial_file, "S.txt"); self.assertEqual(set(self.ctx.security.id_map), {"7e2", "785"})
        self.ops.settings_set("log", {"keep_days": 3}); self.assertEqual(self.ctx.logcfg.keep_days, 3)

    def test_settings_object_identity_is_kept(self):
        sec, dc = self.ctx.security, self.ctx.diag_cfg                                       # windows hold these objects
        self.ops.settings_set("security", {"serial_did": "F1 8D"}); self.ops.settings_set("diag", {"settings": {"bs": 8}})
        self.assertIs(self.ctx.security, sec); self.assertIs(self.ctx.diag_cfg, dc); self.assertEqual(dc.settings.bs, 8)

    def test_new_address_is_used_by_next_request(self):
        self.up(); self.ops.settings_set("diag", {"addr": {"phys": "7E3", "resp": "7EB"}})
        r = self.ops.uds_request(0, "10 01"); self.assertEqual(r["phys"], "7E3")


class GlobalKey(Base):
    def test_unlock_uses_global_key_dll(self):
        self.up()
        with self.assertRaises(OpsError) as cm: self.ops.security_unlock(0)                    # nothing configured, no dll given
        self.assertIn("key_dll", str(cm.exception))
        self.ops.settings_set("security", {"key_dll": "builtin:xor"})
        self.assertEqual(self.ctx.security.key_dll, "builtin:xor")
        self.ops.session_control(0, "03")
        r = self.ops.security_unlock(0); self.assertTrue(r["ok"], r)                          # global one, no dll argument
        self.assertEqual(self.ops.settings_get("security")["security"]["key_dll"], "builtin:xor")


class FileTransferOps(Base):
    """ft_* through the ops layer: the sender and receiver jobs run side by side on in-memory cables (channels 0/1 <-> 2/3)."""
    def setUp(self):
        super().setUp()
        import sys
        sys.path.insert(0, os.path.dirname(__file__))
        from test_cft import Cable
        self.cables = {0: Cable(1), 1: Cable(2)}
        self.released = []

        def fake(ch):
            c = self.cables[ch % 2]
            return (c.s if ch < 2 else c.r), True, True, lambda: self.released.append(ch)
        self.ops._ft_open = fake

    def wait(self, role, secs=40):
        end = time.time() + secs
        while time.time() < end:
            st = self.ops.ft_status()["jobs"].get(role)
            if st and not st["running"]:
                return st
            time.sleep(0.05)
        self.fail("transfer did not finish")

    def test_send_and_receive_through_ops(self):
        src = os.path.join(self.ctx.dir, "src_ft"); dst = os.path.join(self.ctx.dir, "dst_ft")
        os.makedirs(os.path.join(src, "sub"))
        for rel, n in (("a.bin", 30_000), ("sub/b.txt", 777)):
            with open(os.path.join(src, *rel.split("/")), "wb") as f: f.write(os.urandom(n))
        r = self.ops.ft_receive([2, 3], dst); self.assertTrue(r["started"], r)
        s = self.ops.ft_send([0, 1], [src]); self.assertTrue(s["started"], s)
        st = self.wait("send"); rv = self.wait("receive")
        self.assertEqual((st["state"], st["files_done"]), ("done", 2), st["error"])
        self.assertEqual(rv["state"], "done")
        for rel in ("a.bin", "sub/b.txt"):
            with open(os.path.join(src, *rel.split("/")), "rb") as a, open(os.path.join(dst, "src_ft", *rel.split("/")), "rb") as b:
                self.assertEqual(a.read(), b.read())
        self.assertEqual(sorted(self.released), [0, 1, 2, 3])                                   # channels handed back
        self.assertEqual(self.ops.ft_status()["active"], [])

    def test_argument_errors_and_busy_channel(self):
        with self.assertRaises(OpsError): self.ops.ft_send([0], [])
        with self.assertRaises(OpsError): self.ops.ft_send([0, 0], [self.ctx.dir])
        with self.assertRaises(OpsError): self.ops.ft_send([0], ["/no/such/path"])
        with self.assertRaises(OpsError): self.ops.ft_receive([0], "")
        with self.assertRaises(OpsError): self.ops.ft_receive([0], self.ctx.dir, base_id="zz")
        d = os.path.join(self.ctx.dir, "wait_dst")
        self.assertTrue(self.ops.ft_receive([2], d, once=False)["started"])
        with self.assertRaises(OpsError): self.ops.ft_receive([3], d)                           # one receive job at a time
        with self.assertRaises(OpsError): self.ops.ft_send([2], [self.ctx.dir])                 # channel 2 is busy
        self.assertEqual(self.ops.ft_status()["active"], ["receive"])
        self.assertIn("已停止 1", self.ops.ft_stop()["message"]); self.assertEqual(self.ops.ft_status()["active"], [])


class Api(unittest.TestCase):
    def test_methods_exist(self):
        for m in METHODS: self.assertTrue(callable(getattr(Ops, m)), m)


if __name__ == "__main__":
    unittest.main(verbosity=2)
