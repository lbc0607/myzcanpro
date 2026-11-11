"""Headless CanVDS: the operations layer behind the local RPC port, without any window.

    python -m zdiag.daemon [--home DIR] [--port 0] [--token T]
Prints the endpoint and keeps running until Ctrl-C.  Use the 32-bit Python that can load zlgcan.dll for real devices.
The MCP server finds it through <home>/CanVDS_rpc.json (or CANVDS_URL / CANVDS_TOKEN) and starts one itself when none runs."""
import argparse
import signal
import sys
import threading
from typing import List, Optional

from .ops import Ops
from .rpc import RpcServer


def main(argv: Optional[List[str]] = None) -> int:
    for st in (sys.stdout, sys.stderr):                      # Windows pipes default to the ANSI code page
        try:
            st.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(prog="python -m zdiag.daemon", description="CanVDS 无界面服务（本机接口）")
    ap.add_argument("--home", default=None, help="放 scan_data/配置文件的文件夹（默认 CANVDS_HOME 或程序目录）")
    ap.add_argument("--port", type=int, default=0, help="0 = 自动选择")
    ap.add_argument("--token", default="", help="不填则随机生成")
    a = ap.parse_args(argv)
    from .paths import home_dir
    from zdiag_ui.appctx import AppContext
    home = home_dir(a.home)
    ctx = AppContext(home)
    ops = Ops(ctx)
    try:
        srv = RpcServer(ops, home, port=a.port, token=a.token)
        srv.start()
    except (RuntimeError, OSError, ValueError) as e:
        print(f"启动失败: {e}", file=sys.stderr)
        ctx.dev.shutdown()
        return 2
    print(f"CanVDS daemon ready: {srv.url}  (endpoint file: {home})", flush=True)
    stop = threading.Event()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, lambda *_: stop.set())
        except (ValueError, OSError):
            pass
    try:
        stop.wait()
    finally:
        srv.stop()
        ops.shutdown()
        ctx.dev.shutdown()
        ctx.save()
    return 0


if __name__ == "__main__":
    sys.exit(main())
