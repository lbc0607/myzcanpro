"""CanVDS 流程编排 MCP server (stdio).  Lets any MCP-capable agent build, validate, save and run flows.

    pip install -r requirements-mcp.txt          (Python >= 3.10)
    python mcp_server.py

Environment:
    CANVDS_HOME    folder with scan_data/ (default: this folder)
    CANVDS_FLOW_DIR  where flows are saved (default: <CANVDS_HOME>/flows)
    CANVDS_PYTHON  interpreter used by run_flow (default: this one).  Point it at the 32-bit Python that can load
                   zlgcan.dll when this server itself runs on a 64-bit / newer Python.
"""
import functools
import json
import os
import subprocess
import sys
import tempfile
from typing import Any, Dict, Optional, Union

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("CANVDS_HOME", HERE)

from zdiag import flowapi                                  # noqa: E402
from zdiag.flow import FlowError                           # noqa: E402

try:
    from mcp.server.mcpserver import MCPServer             # mcp >= 2
except ImportError:                                        # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer    # type: ignore

mcp = MCPServer("canvds-flow", instructions=(
    "Build CanVDS diagnostic flows. Call get_flow_format and get_catalog first, write the flow JSON, fix it with "
    "validate_flow until ok, then save_flow. run_flow touches real hardware unless simulate=true: ask the user first."))

FlowArg = Union[Dict[str, Any], str]


def _guard(fn):
    @functools.wraps(fn)                                   # keeps the signature the MCP schema is built from
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except FlowError as e:
            return {"ok": False, "error": str(e)}
    return w


@mcp.tool()
def get_flow_format() -> str:
    """Describe the flow JSON format (structure, node types and fields, conditions) with an example. Read this first."""
    return flowapi.FORMAT_DOC


@mcp.tool()
@_guard
def get_catalog() -> Dict[str, Any]:
    """List what exists on this machine: ECU modules and their functions, scan fields, devices, operators, limits.
    Flows may only reference module / function names listed here."""
    return flowapi.catalog()


@mcp.tool()
@_guard
def validate_flow(flow: FlowArg) -> Dict[str, Any]:
    """Validate a flow (JSON object or text). Returns ok, a list of errors to fix, and an indented outline of the steps."""
    return flowapi.check(flow)


@mcp.tool()
@_guard
def save_flow(name: str, flow: FlowArg, overwrite: bool = False, allow_invalid: bool = False) -> Dict[str, Any]:
    """Save a flow as flows/<name>.json (name has no folders). Only valid flows are saved unless allow_invalid=true (draft).
    The user then opens it in CanVDS 流程编排 → 打开."""
    return flowapi.save(flow, name, None, overwrite, allow_invalid)


@mcp.tool()
@_guard
def load_flow(name: str) -> Dict[str, Any]:
    """Read a saved flow (JSON + outline), e.g. to modify it."""
    return flowapi.load(name)


@mcp.tool()
@_guard
def list_flows() -> Dict[str, Any]:
    """List the saved flows."""
    return {"flows": flowapi.list_flows()}


@mcp.tool()
@_guard
def run_flow(name: str = "", flow: Optional[FlowArg] = None, simulate: bool = False, timeout_s: float = 300.0) -> Dict[str, Any]:
    """Execute a flow without the GUI and return every step's result. Give a saved `name` or a `flow` object.
    simulate=true replaces 打开设备 with the simulated ECU (safe dry run). Without it the flow operates the REAL
    device and bus (and excludes the CanVDS window from using it): get the user's consent first. Blocks until done."""
    if flow is None:
        flow = flowapi.load(name)["flow"]
    elif isinstance(flow, str):
        flow = json.loads(flow)
    fd, tmp = tempfile.mkstemp(suffix=".json", prefix="canvds_run_")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(flow, f, ensure_ascii=False)
        cmd = [os.environ.get("CANVDS_PYTHON") or sys.executable, "-m", "zdiag.cli", "run", tmp,
               "--timeout", str(timeout_s)] + (["--simulate"] if simulate else [])
        env = dict(os.environ, PYTHONIOENCODING="utf-8")
        try:
            p = subprocess.run(cmd, cwd=HERE, env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=timeout_s + 60)
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": "执行超时，子进程已被终止"}
        out = p.stdout.decode("utf-8", "replace")
        try:
            return json.loads(out)
        except ValueError:
            return {"ok": False, "error": "执行进程没有返回结果", "stderr": p.stderr.decode("utf-8", "replace")[-1500:]}
    finally:
        try:
            os.remove(tmp)
        except OSError:
            pass


if __name__ == "__main__":
    mcp.run()
