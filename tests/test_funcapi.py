"""Creating modules / 功能 from outside the GUI, trace analysis, and the whole trace -> function -> run workflow."""
import copy, json, os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, os.path.dirname(__file__))
from test_flashgen import Session, record_sim_flash
from zdiag import funcapi
from zdiag.funcapi import FuncError
from zdiag.ops import Ops, OpsError
from zdiag.project import SIM_DEVICE
from zdiag.scanlib import ScanStore
from zdiag_ui.appctx import AppContext

SVC = [{"type": "service", "name": "会话", "pdu": "10 03"}, {"type": "service", "name": "读VIN", "pdu": "22 F1 90"}]


class Base(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        ScanStore(os.path.join(self.home, "scan_data")).ensure_defaults()


class Modules(Base):
    def test_create_module(self):
        r = funcapi.create_module("新ECU", "7E5", home=self.home)
        self.assertEqual((r["module"]["phys"], r["module"]["resp"], r["module"]["func"]), ("7E5", "7ED", "7DF"))
        st = ScanStore(os.path.join(self.home, "scan_data"))
        self.assertIn("新ECU", [m.name for m in st.modules()])
        self.assertTrue(os.path.isfile(st.info_path([m for m in st.modules() if m.name == "新ECU"][0], "code")))
        self.assertEqual(funcapi.create_module("B", "100", "108", "7DF", self.home)["module"]["resp"], "108")
        for bad in (dict(name="新ECU", phys="7E5"), dict(name="a/b", phys="7E5"), dict(name="", phys="1"), dict(name="C", phys="zz"), dict(name="D", phys="1", resp="qq")):
            with self.assertRaises(FuncError, msg=str(bad)): funcapi.create_module(home=self.home, **bad)


class Functions(Base):
    def save(self, path="烧写", steps=None, **kw): return funcapi.save_function("动力域控制器", path, steps if steps is not None else SVC, home=self.home, **kw)

    def test_save_read_delete_and_listing(self):
        r = self.save(); self.assertTrue(r["saved"], r)
        self.assertTrue(os.path.isfile(r["path"])); self.assertIn("功能", r["path"])
        back = funcapi.read_function("动力域控制器", "烧写", self.home)
        self.assertEqual([s["pdu"] for s in back["steps"]], ["10 03", "22 F1 90"]); self.assertEqual(back["steps"][0]["addr"], "phys")   # defaults filled
        self.assertEqual(back["problems"], [])
        self.assertTrue(self.save("标定/零位标定")["saved"]); self.assertTrue(os.path.isdir(os.path.dirname(self.save("标定/零位标定", overwrite=True)["path"])))
        from zdiag.scanlib import function_paths
        st = ScanStore(os.path.join(self.home, "scan_data")); self.assertIn("标定/零位标定", function_paths(st, "动力域控制器"))
        self.assertFalse(self.save()["saved"]); self.assertTrue(self.save(overwrite=True)["saved"])                       # no silent overwrite
        self.assertTrue(funcapi.delete_function("动力域控制器", "烧写", self.home)["deleted"])
        with self.assertRaises(FuncError): funcapi.read_function("动力域控制器", "烧写", self.home)
        with self.assertRaises(FuncError): funcapi.delete_function("动力域控制器", "烧写", self.home)

    def test_path_safety_and_unknown_module(self):
        for evil in ("../x", "a/../../b", "", "/", "..", "a:b", "x*y"):
            with self.assertRaises(FuncError, msg=evil): self.save(evil)
        with self.assertRaises(FuncError): funcapi.save_function("没有", "x", SVC, home=self.home)
        for root, _, files in os.walk(self.home): self.assertFalse([f for f in files if f in ("x.json", "b.json")])

    def test_validation(self):
        for steps in ([], "x", [{"type": "nope"}], [{"name": "no type"}], [{"type": "service", "pdu": ""}], [{"type": "service", "pdu": "zz"}],
                      [{"type": "delay", "ms": -5}], [{"type": "unlock", "level": 2, "dll": "builtin:xor"}]):
            r = self.save("t", steps); self.assertFalse(r["saved"], steps); self.assertTrue(r["problems"])
        incomplete = [{"type": "unlock", "level": 1, "dll": ""}, {"type": "download", "file": ""}]
        r = self.save("草稿", incomplete); self.assertFalse(r["saved"]); self.assertTrue(any("密钥" in p for p in r["problems"]))
        r = self.save("草稿", incomplete, allow_incomplete=True); self.assertTrue(r["saved"]); self.assertTrue(r["problems"])
        self.assertTrue(funcapi.read_function("动力域控制器", "草稿", self.home)["problems"])                                # still reported
        r = self.save("t", [{"type": "service", "pdu": "zz"}], allow_incomplete=True); self.assertTrue(r["saved"] is False or r["problems"])
        for res in ({"parse": "xml"}, {"step": 9}, {"bogus": 1}, {"skip": "x"}, "x"):
            self.assertFalse(self.save("r", SVC, result=res)["saved"], res)
        self.assertTrue(self.save("r", SVC, result={"step": 1, "skip": 3, "parse": "ascii"})["saved"])
        self.assertEqual(funcapi.read_function("动力域控制器", "r", self.home)["result"], {"step": 1, "skip": 3, "parse": "ascii"})
        d = self.save("f1", [{"type": "download", "file": os.path.join(self.home, "nope.bin")}])
        self.assertFalse(d["saved"]); self.assertTrue(any("不存在" in p for p in d["problems"]))


class Trace(Base):
    def test_analyze_text_and_path(self):
        txt = Session().flash().noise().csv()
        r = funcapi.analyze_trace(text=txt)
        self.assertEqual(r["format"], "csv/table"); self.assertEqual(r["suggested_module"], {"phys": "7E2", "resp": "7EA"}); self.assertGreater(r["exchange_count"], 10)
        self.assertTrue(any(e.get("service") == "36 TransferData" for e in r["exchanges"]))                                      # collapsed
        self.assertEqual(r["draft"]["steps"][0]["pdu"], "10 03"); json.dumps(r)
        p = os.path.join(self.home, "t.csv")
        with open(p, "w", encoding="utf-8-sig") as f: f.write(txt)
        self.assertEqual(funcapi.analyze_trace(path=p)["exchange_count"], r["exchange_count"])
        self.assertEqual(funcapi.analyze_trace(text=txt, tester_id="999")["draft"], None)
        for kw in (dict(), dict(text="a", path="b"), dict(path=os.path.join(self.home, "x.exe")), dict(path=os.path.join(self.home, "missing.csv")),
                   dict(text=txt, time_unit="h"), dict(text=txt, tester_id="zz")):
            with self.assertRaises(FuncError, msg=str(kw)): funcapi.analyze_trace(**kw)
        self.assertTrue(funcapi.analyze_trace(text="nothing here")["warnings"])


class Workflow(unittest.TestCase):
    """The whole job: a recorded flash -> analyze -> fill in files / key / CRC -> save as a 功能 -> run it in the background."""

    def test_trace_to_saved_function_to_background_run(self):
        text, plan, d, fw, drv = record_sim_flash()
        home = tempfile.mkdtemp()
        ctx = AppContext(home); ops = Ops(ctx); self.addCleanup(lambda: (ops.shutdown(), ctx.dev.shutdown()))
        r = funcapi.analyze_trace(text=text)
        self.assertTrue(r["draft"] and r["draft"]["todo"], r["warnings"])
        steps = copy.deepcopy(r["draft"]["steps"])
        crc = [s for s in plan.steps if s.get("rkind") == "crc"][0]
        for s in steps:
            if s["type"] == "unlock": s["dll"] = "builtin:xor"
            if s["type"] == "driver": s["file"] = drv
            if s["type"] == "download": s["file"] = fw
            if s.get("rkind") == "crc":
                for k in ("crc", "crc_order", "crc_mode", "rid", "rid_file"): s[k] = crc.get(k, s.get(k))
        steps = [s for s in steps if s["type"] != "delay"]
        mod = funcapi.create_module("烧写模块", r["suggested_module"]["phys"], r["suggested_module"]["resp"], home=home)
        self.assertTrue(mod["created"])
        self.assertFalse(funcapi.save_function("烧写模块", "烧写", r["draft"]["steps"], home=home)["saved"])                       # the raw draft is incomplete
        sv = funcapi.save_function("烧写模块", "烧写", steps, home=home); self.assertTrue(sv["saved"], sv)
        ops.device_open(SIM_DEVICE); ops.channel_start(0)
        ctx.channel.sim_profile = "new"
        with self.assertRaises(OpsError): ops.function_start(0, "烧写模块", "不存在")
        st = ops.function_start(0, "烧写模块", "烧写"); self.assertTrue(st["started"], st)
        with self.assertRaises(OpsError): ops.function_start(0, "烧写模块", "烧写")                                           # one at a time
        for _ in range(300):
            s = ops.function_status()
            if s["state"] == "finished": break
            time.sleep(0.05)
        self.assertEqual((s["state"], s["ok"]), ("finished", True), s)
        self.assertGreater(len(s["steps"]), 5); self.assertTrue(all(x["state"] == "ok" for x in s["steps"]))
        self.assertGreater(s["progress"]["total"], 0); self.assertEqual(ctx.dev.owners, {})

    def test_incomplete_function_is_not_started_and_stop_works(self):
        home = tempfile.mkdtemp(); ctx = AppContext(home); ops = Ops(ctx); self.addCleanup(lambda: (ops.shutdown(), ctx.dev.shutdown()))
        ops.device_open(SIM_DEVICE); ops.channel_start(0)
        self.assertTrue(funcapi.save_function("动力域控制器", "草稿", [{"type": "download", "file": ""}], allow_incomplete=True, home=home)["saved"])
        r = ops.function_start(0, "动力域控制器", "草稿"); self.assertFalse(r["started"]); self.assertTrue(r["problems"])
        self.assertTrue(funcapi.save_function("动力域控制器", "慢", [{"type": "delay", "ms": 30000}], home=home)["saved"])
        self.assertTrue(ops.function_start(0, "动力域控制器", "慢")["started"])
        self.assertEqual(ops.function_status()["state"], "running"); ops.function_stop()
        s = ops.function_status(); self.assertEqual((s["state"], s["ok"]), ("finished", False)); self.assertEqual(ctx.dev.owners, {})
        self.assertEqual(ops.function_stop()["message"], "没有正在执行的功能")


if __name__ == "__main__":
    unittest.main(verbosity=2)
