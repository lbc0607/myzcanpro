"""Agent-facing API of 流程编排 (flowapi, CLI, MCP server)."""
import asyncio, io, json, os, sys, tempfile, unittest, contextlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag import cli, flowapi
from zdiag.flow import FlowError
from zdiag.project import SIM_DEVICE


def flow_doc(module="动力域控制器", extra=None):
    return {"nodes": [
        {"id": "s", "type": "setup", "children": [
            {"id": "o", "type": "open_device", "device": SIM_DEVICE},
            {"id": "c", "type": "start_channel", "channel": 0}]},
        {"id": "l", "type": "lane", "channel": 0, "children": [
            {"id": "sc", "type": "scan", "module": module, "fields": ["code", "dtc"]},
            {"id": "i", "type": "if", "cond": {"ref": "sc", "field": "dtc", "op": "nonempty"},
             "then": [{"id": "d", "type": "delay", "seconds": 0.05}], "else": []}] + (extra or [])}]}


class Api(unittest.TestCase):
    def setUp(self):
        self.h = tempfile.mkdtemp()
        os.environ["CANVDS_FLOW_DIR"] = os.path.join(self.h, "flows")
        self.addCleanup(os.environ.pop, "CANVDS_FLOW_DIR", None)

    def test_catalog_and_format_doc(self):
        c = flowapi.catalog(self.h)
        self.assertIn("动力域控制器", [m["name"] for m in c["modules"]])
        m = [m for m in c["modules"] if m["name"] == "动力域控制器"][0]
        self.assertIn("故障检测", m["functions"]); self.assertIn(SIM_DEVICE, c["devices"])
        self.assertEqual([f["key"] for f in c["scan_fields"]], ["code", "version", "date", "dtc"])
        json.dumps(c)
        for w in ("canvds-flow", "cond", "run_flow", SIM_DEVICE): self.assertIn(w, flowapi.FORMAT_DOC)

    def test_check_accepts_minimal_envelope_and_json_text_and_reports_errors(self):
        self.assertTrue(flowapi.check(flow_doc(), self.h)["ok"])                         # no format / names / enabled
        self.assertTrue(flowapi.check(json.dumps(flow_doc()), self.h)["ok"])
        r = flowapi.check(flow_doc(module="没有这个"), self.h)
        self.assertFalse(r["ok"]); self.assertTrue(any("不存在" in e for e in r["errors"]))
        bad = flow_doc(extra=[{"id": "x", "type": "if", "cond": {"ref": "nope", "op": "ok"}, "then": [], "else": []}])
        self.assertTrue(any("条件" in e for e in flowapi.check(bad, self.h)["errors"]))
        for junk in ("{not json", "[]", {"nodes": "x"}, {"nodes": [{"name": "no type"}]}, {"nodes": [{"type": "zzz"}]}, 5):
            r = flowapi.check(junk, self.h); self.assertFalse(r["ok"], junk); self.assertTrue(r["errors"])
        wrong = flow_doc(extra=[{"id": "w", "type": "delay", "seconds": "abc"}])             # wrong field type: error, not a crash
        self.assertFalse(flowapi.check(wrong, self.h)["ok"])

    def test_outline_shows_ids_and_branches(self):
        o = flowapi.check(flow_doc(), self.h)["outline"]
        for w in ("[sc]", "[i]", "成立时", "否则（空）", "[d]"): self.assertIn(w, o)

    def test_save_load_list_and_name_safety(self):
        r = flowapi.save(flow_doc(), "demo", self.h)
        self.assertTrue(r["saved"]); self.assertTrue(os.path.isfile(r["path"]))
        self.assertTrue(os.path.abspath(r["path"]).startswith(os.path.abspath(flowapi.flow_dir(self.h))))
        self.assertFalse(flowapi.save(flow_doc(), "demo", self.h)["saved"])                  # no silent overwrite
        self.assertTrue(flowapi.save(flow_doc(), "demo.json", self.h, overwrite=True)["saved"])
        self.assertFalse(flowapi.save(flow_doc(module="x"), "bad", self.h)["saved"])        # invalid is not saved...
        self.assertTrue(flowapi.save(flow_doc(module="x"), "draft", self.h, allow_invalid=True)["saved"])   # ...unless a draft
        for evil in ("../evil", "a/b", "..", "", "c:x", "a\\b"):
            with self.assertRaises(FlowError, msg=evil): flowapi.save(flow_doc(), evil, self.h)
        self.assertFalse(os.path.exists(os.path.join(self.h, "evil.json")))
        L = flowapi.load("demo", self.h); self.assertEqual(L["flow"]["name"], "demo"); self.assertIn("[sc]", L["outline"])
        self.assertEqual([f["name"] for f in flowapi.list_flows(self.h)], ["demo", "draft"])
        with self.assertRaises(FlowError): flowapi.load("missing", self.h)

    def test_headless_run_on_simulator(self):
        r = flowapi.run(flow_doc(), self.h, timeout=30, simulate=True)
        self.assertTrue(r["ok"], r); self.assertTrue(r["simulated"])
        by = {s["id"]: s for s in r["steps"]}
        self.assertEqual(by["sc"]["state"], "ok"); self.assertIn("软件编码", by["sc"]["text"]); self.assertEqual(by["l"]["state"], "ok")
        self.assertIn(by["i"]["state"], ("ok", "skip"))
        self.assertEqual(r["failed"], [])
        bad = flowapi.run(flow_doc(module="x"), self.h, simulate=True)                       # invalid flows never execute
        self.assertFalse(bad["ok"]); self.assertEqual(bad["steps"], [])

    def test_run_reports_failure_and_times_out(self):
        doc = flow_doc(extra=[{"id": "u", "type": "uds", "module": "动力域控制器", "pdu": "22 FF FF"}])
        r = flowapi.run(doc, self.h, timeout=30, simulate=True)
        self.assertIsInstance(r["ok"], bool)
        slow = {"nodes": [{"id": "l", "type": "lane", "channel": 0, "children": [{"id": "w", "type": "delay", "seconds": 60}]}]}
        slow["nodes"].insert(0, {"id": "s", "type": "setup", "children": [{"id": "o", "type": "open_device", "device": SIM_DEVICE},
                                                                          {"id": "c", "type": "start_channel", "channel": 0}]})
        r = flowapi.run(slow, self.h, timeout=1, simulate=True)
        self.assertFalse(r["ok"]); self.assertIn("超时", r["message"])


class Cli(unittest.TestCase):
    def call(self, *argv, stdin=None):
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            old = sys.stdin
            if stdin is not None: sys.stdin = io.StringIO(stdin)
            try: code = cli.main(list(argv))
            finally: sys.stdin = old
        return code, out.getvalue()

    def test_commands(self):
        h = tempfile.mkdtemp(); p = os.path.join(h, "f.json"); json.dump(flow_doc(), open(p, "w", encoding="utf-8"))
        code, out = self.call("check", p, "--home", h); self.assertEqual(code, 0); self.assertTrue(json.loads(out)["ok"])
        code, out = self.call("check", "-", "--home", h, stdin=json.dumps(flow_doc(module="x")))
        self.assertEqual(code, 1); self.assertFalse(json.loads(out)["ok"])
        code, out = self.call("outline", p, "--home", h); self.assertIn("[sc]", out)
        code, out = self.call("catalog", "--home", h); self.assertIn("modules", json.loads(out))
        code, out = self.call("format"); self.assertIn("canvds-flow", out)
        code, out = self.call("check", os.path.join(h, "none.json"), "--home", h); self.assertEqual(code, 2)
        code, out = self.call("run", p, "--home", h, "--simulate", "--timeout", "30")
        self.assertEqual(code, 0, out); self.assertTrue(json.loads(out)["ok"])


try:
    import mcp_server
    HAVE_MCP = True
except Exception:                                       # mcp needs Python >= 3.10
    HAVE_MCP = False


@unittest.skipUnless(HAVE_MCP, "mcp SDK not installed")
class McpServer(unittest.TestCase):
    def test_tools_registered_and_callable(self):
        async def go():
            tools = await mcp_server.mcp.list_tools()
            tools = getattr(tools, "tools", tools)
            names = {t.name for t in tools}
            r = await mcp_server.mcp.call_tool("validate_flow", {"flow": flow_doc(module="x")})
            return names, tools, r
        names, tools, r = asyncio.run(go())
        self.assertEqual(names, {"get_flow_format", "get_catalog", "validate_flow", "save_flow", "load_flow", "list_flows", "run_flow"})
        sch = {t.name: (getattr(t, "inputSchema", None) or getattr(t, "input_schema", {})) for t in tools}
        self.assertIn("flow", sch["validate_flow"].get("properties", {})); self.assertIn("name", sch["save_flow"].get("properties", {}))
        self.assertIn("simulate", sch["run_flow"].get("properties", {}))
        self.assertIn("不存在", json.dumps(getattr(r, "structuredContent", None) or str(r), ensure_ascii=False))


if __name__ == "__main__":
    unittest.main(verbosity=2)
