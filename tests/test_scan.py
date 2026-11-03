import json, os, sys, tempfile, threading, unittest
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from zdiag.busstats import ChannelCfg
from zdiag.devmgr import DeviceManager
from zdiag.engine import DiagSession
from zdiag.project import DiagConfig, SIM_DEVICE, new_step
from zdiag.scanlib import (Executor, Module, ScanError, ScanStore, cfg_for, default_info_executor, extract_result,
                           run_executor, scan_module)


class StoreTests(unittest.TestCase):
    def setUp(self):
        self.st = ScanStore(tempfile.mkdtemp()); self.st.ensure_defaults()

    def test_defaults_seeded_once_with_addresses(self):
        ms = self.st.modules()
        self.assertEqual([(m.name, m.phys, m.resp_id, m.func) for m in ms],
                         [("动力域控制器", 0x7E2, 0x7EA, 0x7DF), ("驱动电机控制器", 0x7E3, 0x7EB, 0x7DF),
                          ("电驱充电模块", 0x7E3, 0x7EB, 0x7DF), ("从驱动电机控制器", 0x745, 0x74D, 0x7DF)])
        ms[0].phys = 0x7E0; self.st.save_module(ms[0]); self.st.ensure_defaults()       # not re-seeded / overwritten
        self.assertEqual(self.st.modules()[0].phys, 0x7E0)
        self.assertEqual(self.st.modules()[0].resp_id, 0x7EA)                           # response was stored explicitly
        self.assertEqual([e.name for e in self.st.list_entries(self.st.func_dir(ms[0]))], ["故障检测", "清除故障"])

    def test_cfg_for_replaces_only_addresses(self):
        base = DiagConfig(); base.settings.p2_ms = 1234
        c = cfg_for(base, self.st.modules()[3])
        self.assertEqual((c.addr.phys, c.addr.resp, c.addr.func, c.settings.p2_ms), (0x745, 0x74D, 0x7DF, 1234))
        self.assertEqual(base.addr.phys, 0x7E2)                                          # original untouched

    def test_tree_operations_and_validation(self):
        m = self.st.modules()[0]; root = self.st.func_dir(m)
        sub = self.st.make_folder(root, "标定")
        ex = Executor("零位标定"); ex.steps.append(new_step("service", name="x", pdu="31 01 F0 01"))
        p = self.st.save_new(sub, "零位标定", ex)
        self.assertTrue(os.path.isfile(p))
        self.assertEqual([(e.name, e.is_dir) for e in self.st.list_entries(root)],
                         [("标定", True), ("故障检测", False), ("清除故障", False)])      # folders first
        for bad in ("", "  ", "a/b", "a:b", ".."):
            with self.assertRaises(ScanError): self.st.make_folder(root, bad)
        with self.assertRaises(ScanError): self.st.make_folder(root, "标定")
        with self.assertRaises(ScanError): self.st.save_new(root, "故障检测", Executor())
        with self.assertRaises(ScanError): self.st.make_folder(os.path.join(self.st.root, ".."), "evil")
        ent = [e for e in self.st.list_entries(sub)][0]
        newp = self.st.rename(ent, "零位")
        self.assertEqual(Executor.load(newp).name, "零位")
        self.st.delete([e for e in self.st.list_entries(root) if e.name == "标定"][0])
        self.assertEqual([e.name for e in self.st.list_entries(root)], ["故障检测", "清除故障"])

    def test_executor_roundtrip_and_bad_file(self):
        ex = default_info_executor("dtc"); p = os.path.join(self.st.root, "e.json"); ex.save(p)
        back = Executor.load(p)
        self.assertEqual((back.steps[1]["pdu"], back.result["skip"]), ("19 01 08", 4))
        with open(p, "w", encoding="utf-8") as f: f.write(json.dumps({"format": "other"}))
        with self.assertRaises(ScanError): Executor.load(p)
        with open(p, "w", encoding="utf-8") as f: f.write("{nope")
        with self.assertRaises(ScanError): Executor.load(p)


class ExtractTests(unittest.TestCase):
    def test_skip_auto_explicit_and_step_choice(self):
        ex = Executor("x"); ex.steps.extend([new_step("service", pdu="10 03"), new_step("service", pdu="22 F1 90")])
        replies = {0: bytes([0x50, 3, 0, 0x32, 1, 0xF4]), 1: b"\x62\xF1\x90" + b"ABC"}
        self.assertEqual(extract_result(ex, replies), "41 42 43")                        # last reply, 62 F1 90 stripped
        ex.result["step"] = 0; self.assertEqual(extract_result(ex, replies), "03 00 32 01 F4")   # auto skip 1 for 10
        ex.result.update(step=1, skip=0); self.assertEqual(extract_result(ex, replies), "62 F1 90 41 42 43")
        self.assertIsNone(extract_result(ex, {}))
        ex.result.update(step=1, skip=9); self.assertEqual(extract_result(ex, replies), "")


class ScanOnSim(unittest.TestCase):
    def setUp(self):
        self.st = ScanStore(tempfile.mkdtemp()); self.st.ensure_defaults()
        self.dev = DeviceManager(tempfile.mkdtemp()); self.addCleanup(self.dev.shutdown)
        self.base = DiagConfig(); self.base.settings.p2_ms = 800
        self.dev.open(SIM_DEVICE)
        self.dev.start_channel(0, ChannelCfg(), self.base.addr, "new", self.base.settings.rx_padding)

    def open_session(self, m):
        cfg = cfg_for(self.base, m)
        return DiagSession(self.dev.acquire(0, self, cfg.addr, "new", cfg.settings.rx_padding), cfg)

    def test_scan_all_modules(self):
        got = {m.name: scan_module(self.st, m, self.open_session, threading.Event()) for m in self.st.modules()}
        a = got["动力域控制器"]
        self.assertEqual(bytes.fromhex(a["code"]), b"SW7E20001")           # simulated identity depends on the address
        self.assertEqual((bytes.fromhex(a["version"]), a["dtc"]), (b"V1.08", "00 02"))
        self.assertEqual(got["驱动电机控制器"], got["电驱充电模块"])             # same physical address -> same software
        self.assertNotEqual(got["驱动电机控制器"]["code"], a["code"])
        self.assertEqual(got["从驱动电机控制器"]["dtc"], "00 01")
        self.assertTrue(all(v != "-" for g in got.values() for v in g.values()))

    def test_failed_field_is_dash_others_unaffected_and_progress_callback(self):
        m = self.st.modules()[0]
        ex = self.st.load_info(m, "version"); ex.steps[-1]["pdu"] = "22 F1 77"      # DID the ECU does not have
        self.st.save_info(m, "version", ex)
        seen = []
        res = scan_module(self.st, m, self.open_session, threading.Event(), lambda k, t: seen.append((k, t)))
        self.assertEqual((res["version"], res["code"] != "-", res["dtc"]), ("-", True, "00 02"))
        self.assertEqual(seen[:4], [("code", "-"), ("version", "-"), ("date", "-"), ("dtc", "-")])   # all "-" first
        self.assertIn(("version", "-"), seen[4:])

    def test_cancel_leaves_dashes(self):
        ev = threading.Event(); ev.set()
        res = scan_module(self.st, self.st.modules()[0], self.open_session, ev)
        self.assertEqual(set(res.values()), {"-"})

    def test_function_executor_returns_dtc_list_and_clear(self):
        m = self.st.modules()[0]; fdir = self.st.func_dir(m)
        s = self.open_session(m)
        out = run_executor(s, Executor.load(os.path.join(fdir, "故障检测.json")))
        self.assertTrue(out.ok); self.assertEqual(out.text, "01 23 45 08 01 23 45 08")
        out = run_executor(s, Executor.load(os.path.join(fdir, "清除故障.json")))
        self.assertTrue(out.ok)
        out = run_executor(s, Executor.load(os.path.join(fdir, "故障检测.json")))
        self.assertEqual(out.text, "")                                                  # empty list after clearing
        bad = Executor("bad"); bad.steps.append(new_step("service", pdu="22 F1 77"))
        out = run_executor(s, bad)
        self.assertFalse(out.ok); self.assertIn("NRC", out.error)
        self.assertFalse(run_executor(s, Executor("empty")).ok)


if __name__ == "__main__":
    unittest.main()
