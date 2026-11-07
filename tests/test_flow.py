"""流程编排: model, validation, conditions and the runner against the simulated device (real AppContext)."""
import os, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.flow import Flow, FlowError, FieldRes, NodeResult, eval_cond, to_number, validate, describe, walk
from zdiag.flowrun import CtxEnv, FlowRunner
from zdiag.project import SIM_DEVICE
from zdiag_ui.appctx import AppContext


def cond(ref, op, value="", field=""):
    return {"ref": ref, "field": field, "op": op, "value": value}


class Base(unittest.TestCase):
    def setUp(self):
        self.ctx = AppContext(tempfile.mkdtemp())
        self.env = CtxEnv(self.ctx)
        self.events = []
        self.f = Flow("t")

    def tearDown(self):
        self.ctx.dev.shutdown()

    def mk(self, kind, parent=None, into="children", **p):
        n = self.f.make(kind, **p)
        (parent[into] if parent is not None else self.f.nodes).append(n)
        return n

    def prepare(self, chans=(0,)):
        s = self.f.ensure_setup()
        self.mk("open_device", s, device=SIM_DEVICE)
        for c in chans:
            self.mk("start_channel", s, channel=c)

    def run_flow(self, **kw):
        r = FlowRunner(self.f, self.env, on_event=lambda e: self.events.append(e), **kw)
        return r.run()

    def states(self, node):
        return [e.state for e in self.events if e.node == node["id"]]

    def last(self, node):
        return [e for e in self.events if e.node == node["id"]][-1]


class ModelTests(unittest.TestCase):
    def test_roundtrip_ids_and_repair(self):
        f = Flow(); l = f.make("lane"); f.nodes.append(l)
        i = f.make("if"); l["children"].append(i); i["then"].append(f.make("delay"))
        g = Flow.from_dict(f.to_dict())
        self.assertEqual([n["id"] for n in g.walk()], [n["id"] for n in f.walk()])
        nid = g.new_id(); self.assertNotIn(nid, [n["id"] for n in g.walk()])
        d = f.to_dict(); d["nodes"][0]["children"][0]["id"] = d["nodes"][0]["id"]       # duplicate id
        self.assertEqual(len({n["id"] for n in Flow.from_dict(d).walk()}), 3)
        with self.assertRaises(FlowError): Flow.from_dict({"format": "x"})
        with self.assertRaises(FlowError): Flow.from_dict({"format": "canvds-flow", "nodes": [{"type": "zzz"}]})
        p = os.path.join(tempfile.mkdtemp(), "a", "x.json"); f.save(p); self.assertEqual(Flow.load(p).name, f.name)
        with self.assertRaises(FlowError): Flow.load(p + ".missing")

    def test_locate_and_adopt(self):
        f = Flow(); l = f.make("lane"); f.nodes.append(l); d = f.make("delay"); l["children"].append(d)
        lst, i = f.locate(d["id"]); self.assertIs(lst, l["children"]); self.assertEqual(i, 0)
        import copy
        c = f.adopt(copy.deepcopy(l)); self.assertNotEqual(c["id"], l["id"]); self.assertNotEqual(c["children"][0]["id"], d["id"])

    def test_numbers_and_conditions(self):
        self.assertEqual(to_number("00 02", b"\x00\x02"), 2); self.assertEqual(to_number("12"), 12); self.assertEqual(to_number("0x10"), 16)
        self.assertIsNone(to_number("abc")); self.assertIsNone(to_number("-"))
        R = {"a": NodeResult(True, "00 02", b"\x00\x02"), "b": NodeResult(False, "", b"", error="x"),
             "s": NodeResult(True, "x", fields={"dtc": FieldRes("00 00", b"\x00\x00", True), "code": FieldRes("-", b"", False)}),
             "k": NodeResult(skipped=True), "v": NodeResult(True, "LSVAB123")}
        ev = lambda *a, **k: eval_cond(cond(*a, **k), R)[0]
        self.assertTrue(ev("a", "gt", "0")); self.assertTrue(ev("a", "eq", "2")); self.assertFalse(ev("a", "lt", "2")); self.assertTrue(ev("a", "ge", "2"))
        self.assertTrue(ev("b", "failed")); self.assertFalse(ev("b", "ok")); self.assertTrue(ev("b", "empty")); self.assertTrue(ev("a", "nonempty"))
        self.assertFalse(ev("s", "gt", "0", field="dtc")); self.assertTrue(ev("s", "failed", field="code")); self.assertTrue(ev("s", "empty", field="code"))
        self.assertTrue(ev("v", "contains", "ab1")); self.assertTrue(ev("v", "ne", "x")); self.assertTrue(ev("a", "eq", "00  02"))
        for bad in (("zz", "ok"), ("k", "ok"), ("s", "ok", "", "nope"), ("v", "gt", "1"), ("a", "weird")):
            with self.assertRaises(FlowError): eval_cond(cond(*bad[:3], field=bad[3] if len(bad) > 3 else ""), R)


class ValidationTests(Base):
    def test_validation_messages(self):
        self.assertTrue(any("没有泳道" in e for e in validate(self.f, self.env.store)))
        l0, l1 = self.mk("lane", channel=0), self.mk("lane", channel=0, name="again")
        self.assertTrue(any("同一个通道" in e for e in validate(self.f, self.env.store)))
        l1["channel"] = 1
        self.assertEqual(validate(self.f, self.env.store), [])
        bad = [self.mk("scan", l0, module="不存在"), self.mk("function", l0, module="动力域控制器", path="没有"),
               self.mk("uds", l0, module="动力域控制器", pdu="zz"), self.mk("delay", l0, seconds=1e9),
               self.mk("send", l0, key="", period_ms=0), self.mk("loop", l0, count=0), self.mk("if", l0)]
        errs = "\n".join(validate(self.f, self.env.store))
        for key in ("不存在", "没有", "请求报文无效", "延时", "周期发送参数无效", "循环次数", "没有选择有效的步骤"):
            self.assertIn(key, errs)
        # condition referencing a later step / a container / missing value
        l1["children"] = []; l0["children"] = []
        c = self.mk("if", l0); d = self.mk("note", l0)
        c["cond"] = cond(d["id"], "ok"); self.assertTrue(any("不产生结果" in e for e in validate(self.f, self.env.store)))
        u = self.mk("uds", l0, module="动力域控制器"); c["cond"] = cond(u["id"], "eq", "")
        self.assertTrue(any("缺少比较值" in e for e in validate(self.f, self.env.store)))
        l0["children"] = [u, c]
        c["cond"] = cond(u["id"], "ok"); self.assertEqual(validate(self.f, self.env.store), [])
        l0["children"] = [c, u]; self.assertTrue(any("排在它后面" in e for e in validate(self.f, self.env.store)))
        s = self.f.ensure_setup(); self.mk("scan", s, module="动力域控制器")
        self.assertTrue(any("不能放在" in e for e in validate(self.f, self.env.store)))

    def test_describe_all_types(self):
        for k in ("setup", "lane", "open_device", "start_channel", "scan", "function", "uds", "delay", "send", "stop_send", "if", "loop", "note"):
            self.assertIsInstance(describe(self.f.make(k)), str)


class RunnerTests(Base):
    def test_user_example_end_to_end(self):
        """prepare; lane0: scan 2 modules, read/clear DTC, reset, delay, re-read; lane1: cyclic send meanwhile."""
        self.prepare((0, 1))
        l0, l1 = self.mk("lane", channel=0), self.mk("lane", channel=1)
        scans = {}
        for name in ("动力域控制器", "驱动电机控制器"):
            scans[name] = self.mk("scan", l0, module=name, name="扫描 " + name)
        pdu = self.mk("uds", l0, module="动力域控制器", pdu="10 03 ", name="session")
        rd1 = self.mk("function", l0, module="动力域控制器", path="故障检测", name="读故障码")
        iff = self.mk("if", l0, name="有故障码?", cond=cond(scans["动力域控制器"]["id"], "gt", "0", "dtc"))
        clr = self.mk("function", iff, into="then", module="动力域控制器", path="清除故障", name="清除")
        nothing = self.mk("uds", iff, into="else", module="动力域控制器", pdu="31 01 FF 00", name="不该执行")
        rst = self.mk("uds", l0, module="动力域控制器", pdu="11 01", name="复位")
        dly = self.mk("delay", l0, seconds=0.3)
        rd2 = self.mk("function", l0, module="动力域控制器", path="故障检测", name="复位后重读")
        snd = self.mk("send", l1, key="c1", can_id="321", data="01 02 03 04", period_ms=20)
        d2 = self.mk("delay", l1, seconds=0.4)
        stp = self.mk("stop_send", l1, key="c1")
        rep = self.run_flow()
        self.assertTrue(rep.ok, rep.message)
        for n in (snd, d2, stp, dly, rst, rd1, rd2, clr, iff, *scans.values()):
            self.assertEqual(self.last(n).state, "ok", n["name"])
        self.assertEqual(self.states(nothing), ["skip"])                                  # branch not taken
        self.assertIn("故障码数量=", self.last(scans["动力域控制器"]).text)
        self.assertNotEqual(rep.results[rd1["id"]].text, "完成（无返回数据）")               # 2 DTCs before
        self.assertEqual(rep.results[rd2["id"]].text, "完成（无返回数据）")                  # cleared after
        self.assertTrue(any("已开始" in e.text for e in self.events if e.node == snd["id"])); self.assertRegex(rep.results[snd["id"]].text, r"共发送 \d+ 帧")
        self.assertGreaterEqual(int(rep.results[snd["id"]].text.split("共发送 ")[1].split(" ")[0]), 5)
        self.assertEqual(self.last(l0).state, "ok"); self.assertEqual(self.last(l1).state, "ok")

    def test_skip_clear_when_no_dtc_and_else_branch_on_failure(self):
        self.prepare()
        l = self.mk("lane")
        sc = self.mk("scan", l, module="动力域控制器")
        rd = self.mk("function", l, module="动力域控制器", path="故障检测")
        clr0 = self.mk("function", l, module="动力域控制器", path="清除故障")                # make DTC count 0
        sc2 = self.mk("scan", l, module="动力域控制器", name="再扫描")
        iff = self.mk("if", l, cond=cond(sc2["id"], "gt", "0", "dtc"))
        inner = self.mk("delay", iff, into="then", name="不应执行")
        els = self.mk("delay", iff, into="else", name="应执行", seconds=0.01)
        rep = self.run_flow()
        self.assertTrue(rep.ok, rep.message)
        self.assertEqual((self.states(inner), self.states(els)), (["skip"], ["running", "ok"]))

    def test_failure_goes_to_other_branch(self):
        self.prepare()
        l = self.mk("lane")
        bad = self.mk("uds", l, module="动力域控制器", pdu="2F 00 00 00", name="会失败的请求", on_fail="continue")
        iff = self.mk("if", l, cond=cond(bad["id"], "failed"))
        a = self.mk("delay", iff, into="then", name="失败分支", seconds=0.01)
        b = self.mk("delay", iff, into="else", name="成功分支")
        after = self.mk("uds", l, module="动力域控制器", pdu="3E 00", name="之后继续")
        rep = self.run_flow()
        self.assertEqual(self.last(bad).state, "fail"); self.assertEqual(self.states(a)[-1], "ok"); self.assertEqual(self.states(b), ["skip"])
        self.assertEqual(self.last(after).state, "ok"); self.assertTrue(rep.ok)

    def test_failure_stops_lane_and_marks_rest_skipped(self):
        self.prepare((0, 1))
        l0, l1 = self.mk("lane", channel=0), self.mk("lane", channel=1)
        bad = self.mk("uds", l0, module="动力域控制器", pdu="2F 00 00 00")
        later = self.mk("delay", l0)
        other = self.mk("delay", l1, seconds=0.05)
        rep = self.run_flow()
        self.assertFalse(rep.ok)
        self.assertEqual(self.last(bad).state, "fail"); self.assertEqual(self.last(later).state, "skip"); self.assertEqual(self.last(l0).state, "fail")
        self.assertEqual(self.last(other).state, "ok"); self.assertEqual(self.last(l1).state, "ok")      # the other lane is unaffected

    def test_loops(self):
        self.prepare()
        l = self.mk("lane")
        lp = self.mk("loop", l, mode="count", count=3, name="三次")
        d = self.mk("delay", lp, into="body", seconds=0.0)
        u = self.mk("uds", l, module="动力域控制器", pdu="22 F1 94", pre_session=True, parse="ascii", name="读")
        w = self.mk("loop", l, mode="while", max_iter=4, cond=cond(u["id"], "ok"), name="while")
        d2 = self.mk("delay", w, into="body", seconds=0.0)
        un = self.mk("loop", l, mode="until", max_iter=50, cond=cond(u["id"], "ok"), name="until")
        d3 = self.mk("delay", un, into="body", seconds=0.0)
        rep = self.run_flow()
        self.assertTrue(rep.ok, rep.message)
        self.assertEqual(rep.results[lp["id"]].text, "共执行 3 次"); self.assertEqual(self.states(d).count("reset"), 3)
        self.assertIn("达到上限 4 次", rep.results[w["id"]].text); self.assertEqual(rep.results[un["id"]].text, "共执行 1 次")
        self.assertEqual(rep.results[u["id"]].text, "SW7E20001")                              # ASCII parse of F194

    def test_cancel_stops_everything_and_cleans_up(self):
        self.prepare((0, 1))
        l0, l1 = self.mk("lane", channel=0), self.mk("lane", channel=1)
        self.mk("delay", l0, seconds=30)
        self.mk("send", l1, key="x", period_ms=10)
        self.mk("delay", l1, seconds=30)
        cancel = threading.Event()
        r = FlowRunner(self.f, self.env, cancel=cancel, on_event=lambda e: self.events.append(e))
        out = []
        t = threading.Thread(target=lambda: out.append(r.run())); t.start()
        time.sleep(0.5); cancel.set(); t.join(5)
        self.assertFalse(t.is_alive()); self.assertFalse(out[0].ok); self.assertEqual(out[0].message, "已停止")
        self.assertEqual(self.ctx.dev.owners, {})                                              # buses released
        self.assertFalse([x for x in threading.enumerate() if x.name == "flow-send"])         # sender thread ended

    def test_refuses_invalid_flow_and_setup_failures(self):
        rep = self.run_flow(); self.assertFalse(rep.ok); self.assertIn("没有泳道", rep.message); self.assertEqual(self.events, [])
        self.prepare((0, 1))
        l = self.mk("lane", channel=0); self.mk("delay", l, seconds=0.0)
        self.assertTrue(self.run_flow().ok)
        self.ctx.dev.shutdown()
        self.f.setup()["children"][0]["device"] = "USBCAN-II"; self.ctx.dev.open(SIM_DEVICE)                # different device already open
        self.events.clear(); rep = self.run_flow()
        self.assertFalse(rep.ok); self.assertEqual(rep.message, "准备失败"); self.assertEqual(self.last(self.f.setup()).state, "fail")
        self.assertEqual(self.last(l).state, "skip")

    def test_disabled_nodes_and_lane_without_started_channel(self):
        self.ctx.dev.open(SIM_DEVICE)
        l = self.mk("lane", channel=3)
        sc = self.mk("scan", l, module="动力域控制器")
        rep = self.run_flow()
        self.assertFalse(rep.ok); self.assertIn("通道未启动", self.last(sc).text)
        sc["enabled"] = False; rep = self.run_flow(); self.assertTrue(rep.ok); self.assertEqual(self.last(sc).state, "skip")


if __name__ == "__main__":
    unittest.main(verbosity=2)
