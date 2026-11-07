"""Local RPC port + headless daemon (real sockets on loopback)."""
import json, os, subprocess, sys, tempfile, time, unittest, urllib.request
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.ops import Ops
from zdiag.project import SIM_DEVICE
from zdiag.rpc import RpcError, RpcServer, endpoint_path, ping, read_endpoint, rpc_call
from zdiag_ui.appctx import AppContext

ROOT = os.path.join(os.path.dirname(__file__), "..")


def raw(url, path, body=None, headers=None, method="POST"):
    req = urllib.request.Request(url + path, data=None if body is None else json.dumps(body).encode(), headers=headers or {}, method=method)
    try:
        with urllib.request.urlopen(req, timeout=5) as r: return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e: return e.code, json.loads(e.read() or b"{}")


class Rpc(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.ctx = AppContext(self.home); self.ops = Ops(self.ctx)
        self.srv = RpcServer(self.ops, self.home, token="tok"); self.srv.start()
        self.addCleanup(self.stop)

    def stop(self):
        self.srv.stop(); self.ops.shutdown(); self.ctx.dev.shutdown()

    def call(self, m, **p): return rpc_call(self.srv.url, "tok", m, p)

    def test_endpoint_file_and_ping(self):
        e = read_endpoint(self.home); self.assertEqual((e["url"], e["token"], e["pid"]), (self.srv.url, "tok", os.getpid()))
        self.assertTrue(ping(self.srv.url))
        if os.name == "posix": self.assertEqual(os.stat(endpoint_path(self.home)).st_mode & 0o077, 0)
        self.srv.stop(); self.assertFalse(os.path.exists(endpoint_path(self.home))); self.assertFalse(ping(self.srv.url))
        self.srv = RpcServer(self.ops, self.home, token="tok"); self.srv.start()                 # restartable

    def test_calls_errors_and_auth(self):
        self.call("device_open", name=SIM_DEVICE); self.call("channel_start", channel=0)
        r = self.call("uds_request", channel=0, pdu="10 03"); self.assertTrue(r["ok"])
        with self.assertRaises(RpcError) as c: self.call("uds_request", channel=0, pdu="zz"); self.assertIn("无效", str(c.exception))
        with self.assertRaises(RpcError): self.call("nope")
        with self.assertRaises(RpcError): self.call("uds_request", channel=0)                      # missing argument
        with self.assertRaises(RpcError): self.call("uds_request", channel=0, pdu="10 01", bogus=1)
        with self.assertRaises(RpcError): self.call("shutdown")                                    # only whitelisted methods
        with self.assertRaises(RpcError): rpc_call(self.srv.url, "wrong", "status")
        code, _ = raw(self.srv.url, "/call", {"method": "status"}); self.assertEqual(code, 401)
        code, _ = raw(self.srv.url, "/call", {"method": "status"}, {"Authorization": "Bearer tok", "Origin": "http://evil.example"}); self.assertEqual(code, 403)
        code, _ = raw(self.srv.url, "/call", {"method": "status"}, {"Authorization": "Bearer tok", "Host": "evil.example"}); self.assertEqual(code, 403)
        code, _ = raw(self.srv.url, "/elsewhere", {}); self.assertEqual(code, 404)
        code, d = raw(self.srv.url, "/call", None, {"Authorization": "Bearer tok"}); self.assertEqual(code, 200)   # empty body -> unknown method error
        self.assertFalse(d["ok"])
        self.assertTrue(self.call("status")["is_open"])                                            # server survived all of it

    def test_second_server_refused_and_remote_bind_refused(self):
        with self.assertRaises(RuntimeError): RpcServer(self.ops, self.home).start()
        with self.assertRaises(ValueError): RpcServer(self.ops, self.home, host="0.0.0.0")

    def test_parallel_calls_serialise_bus_access(self):
        import threading
        self.call("device_open", name=SIM_DEVICE); self.call("channel_start", channel=0)
        out, errs = [], []
        def go():
            try: out.append(self.call("uds_request", channel=0, pdu="22 F1 90")["ok"])
            except Exception as e: errs.append(e)
        ts = [threading.Thread(target=go) for _ in range(6)]
        [t.start() for t in ts]; [t.join(20) for t in ts]
        self.assertEqual((errs, out), ([], [True] * 6))
        threading.Thread(target=lambda: self.call("monitor_capture", seconds=1.0), daemon=True).start()
        t0 = time.time(); self.call("status"); self.assertLess(time.time() - t0, 0.8)               # slow free call does not block others


class Daemon(unittest.TestCase):
    def test_daemon_process(self):
        home = tempfile.mkdtemp()
        p = subprocess.Popen([sys.executable, "-m", "zdiag.daemon", "--home", home], cwd=ROOT, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self.addCleanup(lambda: (p.terminate(), p.wait(10), p.stdout.close(), p.stderr.close()))
        e = None
        for _ in range(100):
            e = read_endpoint(home)
            if e and ping(e["url"]): break
            time.sleep(0.1)
        self.assertTrue(e and ping(e["url"]), p.poll())
        r = rpc_call(e["url"], e["token"], "device_open", {"name": SIM_DEVICE}); self.assertTrue(r["is_open"])
        p2 = subprocess.run([sys.executable, "-m", "zdiag.daemon", "--home", home], cwd=ROOT, capture_output=True, timeout=30)
        self.assertEqual(p2.returncode, 2); self.assertIn("已有", p2.stderr.decode("utf-8", "replace"))
        p.terminate(); p.wait(10)
        self.assertIsNone(read_endpoint(home))


if __name__ == "__main__":
    unittest.main(verbosity=2)
