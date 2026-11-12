"""Local RPC port (stdlib only, Python 3.8): exposes `Ops` to agents over HTTP on 127.0.0.1.

    POST /call   {"method": "uds_request", "params": {...}}   header  Authorization: Bearer <token>
    -> {"ok": true, "result": {...}}  |  {"ok": false, "error": "..."}
    GET  /ping   (no auth)  -> {"app": "CanVDS", "pid": ...}

Safety: loopback only, random bearer token, Host must be loopback, browser requests (Origin header) are refused.
The endpoint is written to <home>/CanVDS_rpc.json so a client (the MCP server) finds a running GUI / daemon by itself."""
import hmac
import json
import os
import secrets
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from .ops import METHODS, Ops, OpsError

ENDPOINT_FILE = "CanVDS_rpc.json"
MAX_BODY = 4 * 1024 * 1024
# quick / read-only / must-stay-responsive methods run concurrently; everything that talks on a bus is serialised
FREE = {"ft_status", "ft_stop", "status", "device_list", "modules", "settings_get", "monitor_trace", "monitor_snapshot", "monitor_capture", "monitor_clear",
        "cyclic_list", "cyclic_stop", "keepalive_stop", "record_start", "record_stop", "lin_status", "lin_close"}


def endpoint_path(home: str) -> str:
    return os.path.join(home, ENDPOINT_FILE)


class RpcServer:
    def __init__(self, ops: Ops, home: str, host: str = "127.0.0.1", port: int = 0, token: str = "", on_call=None) -> None:
        if host not in ("127.0.0.1", "localhost", "::1"):
            raise ValueError("只允许绑定本机回环地址")
        self.ops, self.home = ops, home
        self.on_call = on_call                                   # (method, ok) after every call, in the handler thread
        self.token = token or secrets.token_urlsafe(24)
        self._bus_lock = threading.Lock()
        self._httpd = ThreadingHTTPServer((host, port), self._handler())
        self._httpd.daemon_threads = True
        self.host, self.port = self._httpd.server_address[0], self._httpd.server_address[1]
        self._thread: Optional[threading.Thread] = None

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ------------------------------------------------------------------ lifecycle
    def start(self) -> None:
        live = read_endpoint(self.home)
        if live and ping(live["url"]):
            self._httpd.server_close()
            raise RuntimeError(f"已有 CanVDS 接口在运行: {live['url']}（同一设备只能由一个程序占用）")
        self._thread = threading.Thread(target=self._httpd.serve_forever, name="canvds-rpc", daemon=True)
        self._thread.start()
        data = {"url": self.url, "token": self.token, "pid": os.getpid()}
        path = endpoint_path(self.home)
        tmp = path + ".tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        os.replace(tmp, path)

    def stop(self) -> None:
        try:
            cur = read_endpoint(self.home)
            if cur and cur.get("pid") == os.getpid():
                os.remove(endpoint_path(self.home))
        except OSError:
            pass
        self._httpd.shutdown()
        self._httpd.server_close()

    # ------------------------------------------------------------------ dispatch
    def call(self, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
        if method not in METHODS:
            raise OpsError(f"未知方法 {method}，可用: {', '.join(METHODS)}")
        if not isinstance(params, dict):
            raise OpsError("params 应为对象")
        fn = getattr(self.ops, method)
        ok = False
        try:
            if method in FREE:
                res = fn(**params)
            else:
                with self._bus_lock:
                    res = fn(**params)
            ok = True
            return res
        except TypeError as e:                                   # wrong / missing argument names
            raise OpsError(f"{method} 参数不对: {e}")
        finally:
            if self.on_call is not None and method not in ("status", "cyclic_list"):
                try:
                    self.on_call(method, ok)
                except Exception:
                    pass

    def _handler(self):
        srv = self

        class H(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, *a):
                pass

            def _send(self, code: int, obj: Dict[str, Any]) -> None:
                body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _host_ok(self) -> bool:
                h = (self.headers.get("Host") or "").rsplit(":", 1)[0].strip("[]").lower()
                return h in ("127.0.0.1", "localhost", "::1")

            def do_GET(self):
                if self.path == "/ping" and self._host_ok():
                    return self._send(200, {"app": "CanVDS", "pid": os.getpid()})
                self._send(404, {"ok": False, "error": "not found"})

            def do_POST(self):
                if self.path != "/call":
                    return self._send(404, {"ok": False, "error": "not found"})
                if not self._host_ok() or self.headers.get("Origin"):
                    return self._send(403, {"ok": False, "error": "forbidden"})
                got = (self.headers.get("Authorization") or "")
                if not got.startswith("Bearer ") or not hmac.compare_digest(got[7:], srv.token):
                    return self._send(401, {"ok": False, "error": "令牌无效"})
                try:
                    n = int(self.headers.get("Content-Length") or 0)
                    if n > MAX_BODY:
                        return self._send(413, {"ok": False, "error": "请求太大"})
                    req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
                    res = srv.call(str(req.get("method", "")), req.get("params") or {})
                    self._send(200, {"ok": True, "result": res})
                except OpsError as e:
                    self._send(200, {"ok": False, "error": str(e)})
                except (ValueError, UnicodeDecodeError) as e:
                    self._send(400, {"ok": False, "error": f"请求不是有效的 JSON: {e}"})
                except Exception as e:                              # never leak a traceback / kill the server thread
                    self._send(500, {"ok": False, "error": f"内部错误: {type(e).__name__}: {e}"})

        return H


# --------------------------------------------------------------------------- client side
def read_endpoint(home: str) -> Optional[Dict[str, Any]]:
    try:
        with open(endpoint_path(home), "r", encoding="utf-8") as f:
            d = json.load(f)
        return d if isinstance(d, dict) and d.get("url") and d.get("token") else None
    except (OSError, ValueError):
        return None


def ping(url: str, timeout: float = 1.0) -> bool:
    try:
        with urllib.request.urlopen(url + "/ping", timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8")).get("app") == "CanVDS"
    except (OSError, ValueError, urllib.error.URLError):
        return False


class RpcError(Exception):
    pass


def rpc_call(url: str, token: str, method: str, params: Optional[Dict[str, Any]] = None, timeout: float = 180.0) -> Dict[str, Any]:
    req = urllib.request.Request(url + "/call", data=json.dumps({"method": method, "params": params or {}}).encode("utf-8"),
                                 headers={"Content-Type": "application/json", "Authorization": "Bearer " + token}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            d = json.loads(r.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        try:
            d = json.loads(e.read().decode("utf-8"))
        except ValueError:
            raise RpcError(f"HTTP {e.code}")
    except (OSError, urllib.error.URLError) as e:
        raise RpcError(f"连不上 CanVDS 接口 {url}: {e}")
    if not d.get("ok"):
        raise RpcError(d.get("error", "未知错误"))
    return d["result"]
