"""MCP server: tool registration and the bridge to the local port (real RPC server on loopback)."""
import asyncio, json, os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
try:
    import mcp_server as M
    HAVE = True
except Exception:                                        # the mcp SDK needs Python >= 3.10
    HAVE = False
from zdiag.ops import Ops
from zdiag.project import SIM_DEVICE
from zdiag.rpc import RpcServer
from zdiag_ui.appctx import AppContext

EXPECTED = {"lin_open", "lin_close", "lin_request", "lin_scan_nodes", "lin_scan_pump", "get_flow_format", "get_catalog", "validate_flow", "save_flow", "load_flow", "list_flows", "run_flow", "flow_status", "stop_flow",
            "get_status", "device_list", "device_open", "device_close", "channel_start", "channel_stop", "uds_request", "read_did", "write_did",
            "read_dtc", "clear_dtc", "ecu_reset", "session_control", "security_unlock", "keepalive_start", "keepalive_stop", "list_modules",
            "scan_module", "run_function", "send_frame", "cyclic_start", "cyclic_stop", "monitor_trace", "monitor_snapshot", "monitor_capture",
            "monitor_clear", "record_start", "record_stop", "get_settings", "set_settings",
            "get_function_format", "analyze_trace", "create_module", "check_function", "save_function", "read_function", "delete_function",
            "start_function", "function_status", "stop_function"}


def tool(_tool, **args):
    r = asyncio.run(M.mcp.call_tool(_tool, args))
    sc = getattr(r, "structuredContent", None)
    if isinstance(sc, dict):
        return sc.get("result", sc) if set(sc) == {"result"} and isinstance(sc["result"], (dict, str)) else sc
    txt = "".join(getattr(c, "text", "") for c in getattr(r, "content", []))
    try: return json.loads(txt)
    except ValueError: return txt


@unittest.skipUnless(HAVE, "mcp SDK not installed (needs Python >= 3.10)")
class Bridge(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.old = {k: os.environ.get(k) for k in ("CANVDS_HOME", "CANVDS_FLOW_DIR", "CANVDS_URL", "CANVDS_TOKEN", "CANVDS_AUTOSTART")}
        for k in ("CANVDS_URL", "CANVDS_TOKEN"): os.environ.pop(k, None)
        os.environ["CANVDS_HOME"] = self.home; os.environ["CANVDS_FLOW_DIR"] = os.path.join(self.home, "flows")
        self.addCleanup(self.restore)

    def restore(self):
        for k, v in self.old.items():
            if v is None: os.environ.pop(k, None)
            else: os.environ[k] = v

    def serve(self):
        ctx = AppContext(self.home); ops = Ops(ctx); srv = RpcServer(ops, self.home); srv.start()
        self.addCleanup(lambda: (srv.stop(), ops.shutdown(), ctx.dev.shutdown()))
        return ctx

    def test_registered_tools_and_schemas(self):
        tools = asyncio.run(M.mcp.list_tools()); tools = getattr(tools, "tools", tools)
        self.assertEqual({t.name for t in tools}, EXPECTED)
        sch = {t.name: (getattr(t, "inputSchema", None) or getattr(t, "input_schema", {})) for t in tools}
        self.assertEqual(set(sch["uds_request"]["properties"]) >= {"channel", "pdu", "module", "parse"}, True)
        self.assertIn("channel", sch["uds_request"]["required"]); self.assertIn("values", sch["set_settings"]["properties"])
        for t in tools: self.assertTrue((t.description or "").strip(), t.name)

    def test_operations_through_the_bridge(self):
        ctx = self.serve()
        self.assertTrue(tool("device_open", name=SIM_DEVICE)["is_open"])
        self.assertIn("已启动", tool("channel_start", channel=0, protocol="CAN FD")["message"])
        r = tool("uds_request", channel=0, pdu="22 F1 90", parse="ascii"); self.assertTrue(r["ok"], r); self.assertTrue(r["value"])
        bad = tool("uds_request", channel=0, pdu="zz"); self.assertFalse(bad.get("ok", False)); self.assertIn("无效", bad["error"])
        self.assertTrue(tool("scan_module", channel=0, module="动力域控制器", fields=["code"])["ok"])
        self.assertTrue(tool("read_dtc", channel=0)["ok"])
        self.assertTrue(tool("send_frame", channel=0, can_id="321", data="01 02", count=2, interval_ms=2)["ok"])
        self.assertEqual(len(tool("monitor_trace", count=5, ids="321")["frames"]), 2)
        tool("cyclic_start", channel=0, key="k", can_id="100", data="01", period_ms=10)
        self.assertEqual(tool("get_status")["senders"][0]["key"], "k"); tool("cyclic_stop")
        self.assertEqual(tool("set_settings", section="diag", values={"settings": {"p2_ms": 750}})["diag"]["settings"]["p2_ms"], 750)
        self.assertEqual(ctx.diag_cfg.settings.p2_ms, 750); self.assertEqual(tool("get_settings", section="diag")["diag"]["settings"]["p2_ms"], 750)
        self.assertIn("error", tool("set_settings", section="diag", values={"settings": {"p2_ms": 0}}))
        self.assertIn("modules", tool("list_modules"))

    def test_run_flow_waits_and_reports(self):
        self.serve(); tool("device_open", name=SIM_DEVICE); tool("channel_start", channel=0)
        doc = {"nodes": [{"id": "l", "type": "lane", "channel": 0, "children": [
            {"id": "sc", "type": "scan", "module": "动力域控制器", "fields": ["code"]}, {"id": "w", "type": "delay", "seconds": 0.1}]}]}
        st = tool("run_flow", flow=doc, wait_s=20); self.assertEqual((st["state"], st["ok"]), ("finished", True), st)
        saved = tool("save_flow", name="demo", flow=doc); self.assertTrue(saved["saved"])
        st = tool("run_flow", name="demo", wait_s=20); self.assertTrue(st["ok"], st)
        bad = tool("run_flow", flow={"nodes": [{"id": "l", "type": "lane", "channel": 0, "children": [{"id": "s", "type": "scan", "module": "x", "fields": ["code"]}]}]})
        self.assertFalse(bad.get("started", True)); self.assertTrue(bad.get("errors"))
        slow = {"nodes": [{"id": "l", "type": "lane", "channel": 0, "children": [{"id": "w", "type": "delay", "seconds": 30}]}]}
        self.assertEqual(tool("run_flow", flow=slow, wait_s=0.5)["state"], "running")
        self.assertEqual(tool("stop_flow")["state"], "finished")

    def test_trace_to_function_tools(self):
        sys.path.insert(0, os.path.dirname(__file__))
        from test_flashgen import Session
        self.assertIn("service", tool("get_function_format"))
        r = tool("analyze_trace", text=Session().flash().csv()); self.assertTrue(r["draft"]["steps"]); self.assertTrue(r["draft"]["todo"])
        self.assertTrue(tool("create_module", name="烧写ECU", phys=r["suggested_module"]["phys"], resp=r["suggested_module"]["resp"])["created"])
        chk = tool("check_function", steps=r["draft"]["steps"]); self.assertFalse(chk["ok"]); self.assertTrue(any("密钥" in p for p in chk["problems"]))
        sv = tool("save_function", module="烧写ECU", path="烧写草稿", steps=r["draft"]["steps"], allow_incomplete=True); self.assertTrue(sv["saved"])
        self.assertTrue(tool("read_function", module="烧写ECU", path="烧写草稿")["problems"])
        self.assertIn("error", tool("save_function", module="没有", path="x", steps=[{"type": "delay", "ms": 1}]))
        self.assertTrue(tool("delete_function", module="烧写ECU", path="烧写草稿")["deleted"])
        self.assertIn("error", tool("analyze_trace"))

    def test_no_endpoint_without_autostart_is_an_error_not_a_crash(self):
        os.environ["CANVDS_AUTOSTART"] = "0"
        r = tool("get_status"); self.assertIn("error", r); self.assertIn("agent", r["error"])

    def test_autostarts_a_daemon(self):
        os.environ["CANVDS_AUTOSTART"] = "1"; os.environ["CANVDS_PYTHON"] = sys.executable
        self.addCleanup(os.environ.pop, "CANVDS_PYTHON", None); self.addCleanup(M._stop_daemon)
        self.assertTrue(tool("device_open", name=SIM_DEVICE)["is_open"])
        self.assertTrue(tool("get_status")["is_open"])                                  # same daemon again


if __name__ == "__main__":
    unittest.main(verbosity=2)
