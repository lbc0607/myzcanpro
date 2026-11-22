"""Entry points the Android app calls through Chaquopy: start the operations layer + local RPC/web UI inside the app process.

    start(home)            -> JSON {"url": ..., "token": ...}   (idempotent)
    attach_usb(...)/detach_usb()  hand the opened USB connection to the gs_usb driver
    stop()
The page served at "/" is zdiag/webui/index.html; the app loads  <url>/#<token>  in a WebView."""
import json
import os
import pkgutil
import threading

_lock = threading.Lock()
_state = {}


def start(home: str) -> str:
    with _lock:
        if _state:
            return json.dumps(_state["info"])
        os.makedirs(home, exist_ok=True)
        os.environ["CANVDS_HOME"] = home
        from zdiag_ui.appctx import AppContext
        from .ops import Ops
        from .rpc import RpcServer
        ctx = AppContext(home)
        ops = Ops(ctx)
        html = pkgutil.get_data("zdiag", "webui/index.html")
        srv = RpcServer(ops, home, ui_html=html)
        srv.start()
        info = {"url": srv.url, "token": srv.token, "home": home}
        _state.update(ctx=ctx, ops=ops, srv=srv, info=info)
        return json.dumps(info)


def attach_usb(conn, ifnum, ep_in, ep_out, present=None, reader=None) -> None:
    from . import android_usb
    chk = None
    if present is not None:
        chk = lambda: bool(present.getAsBoolean())          # java.util.function.BooleanSupplier
    android_usb.attach(conn, int(ifnum), ep_in, ep_out, chk, reader)


def detach_usb() -> None:
    from . import android_usb
    android_usb.detach()


def stop() -> None:
    with _lock:
        if not _state:
            return
        srv, ops, ctx = _state["srv"], _state["ops"], _state["ctx"]
        _state.clear()
    try:
        srv.stop()
        ops.shutdown()
    except Exception:
        pass
