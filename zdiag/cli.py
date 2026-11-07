"""Command line of 流程编排 for agents / scripts (stdlib only; works with the 32-bit Python that owns the zlgcan.dll).

    python -m zdiag.cli format | catalog | list
    python -m zdiag.cli check FILE | outline FILE | run FILE [--simulate] [--timeout 300]
Options: --home DIR (folder with scan_data/, default CANVDS_HOME or the app folder).  FILE is a flow .json path or '-' (stdin).
Output: one JSON document on stdout.  Exit code 0 = ok, 1 = not ok, 2 = usage / file error."""
import argparse
import io
import json
import sys
from typing import List, Optional

from . import flowapi
from .flow import FlowError


def _read(arg: str) -> str:
    if arg == "-":
        return sys.stdin.read()
    with open(arg, "r", encoding="utf-8") as f:
        return f.read()


def _out(obj) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def main(argv: Optional[List[str]] = None) -> int:
    for s in (sys.stdout, sys.stderr, sys.stdin):
        try:
            s.reconfigure(encoding="utf-8")
        except (AttributeError, ValueError, OSError):
            pass
    ap = argparse.ArgumentParser(prog="python -m zdiag.cli", description="CanVDS 流程编排命令行")
    ap.add_argument("cmd", choices=["format", "catalog", "list", "check", "outline", "run"])
    ap.add_argument("file", nargs="?", help="流程文件路径，或 - 表示标准输入")
    ap.add_argument("--home", default=None)
    ap.add_argument("--simulate", action="store_true", help="run: 把「打开设备」换成模拟ECU试跑")
    ap.add_argument("--timeout", type=float, default=300.0)
    a = ap.parse_args(argv)
    try:
        if a.cmd == "format":
            sys.stdout.write(flowapi.FORMAT_DOC)
            return 0
        if a.cmd == "catalog":
            _out(flowapi.catalog(a.home))
            return 0
        if a.cmd == "list":
            _out(flowapi.list_flows(a.home))
            return 0
        if not a.file:
            ap.error("需要 FILE")
        text = _read(a.file)
        if a.cmd == "check":
            r = flowapi.check(text, a.home)
            _out(r)
            return 0 if r["ok"] else 1
        if a.cmd == "outline":
            sys.stdout.write(flowapi.outline(flowapi.to_flow(text)) + "\n")
            return 0
        real, sys.stdout = sys.stdout, sys.stderr                 # keep stdout clean: library prints go to stderr
        try:
            r = flowapi.run(text, a.home, a.timeout, a.simulate)
        finally:
            sys.stdout = real
        _out(r)
        return 0 if r["ok"] else 1
    except (FlowError, OSError) as e:
        _out({"ok": False, "error": str(e)})
        return 2


if __name__ == "__main__":
    sys.exit(main())
