"""CanVDS MCP server (stdio): lets any MCP-capable agent operate the host - devices / channels, UDS, raw frames, monitor,
settings, 软件扫描 (modules / 功能, incl. creating a 功能 from a recorded trace) and LIN diagnostics.

    pip install -r requirements-mcp.txt          (Python >= 3.10)
    python mcp_server.py

The server is a thin bridge to the CanVDS local port (zdiag.rpc).  It uses, in this order:
  1. CANVDS_URL + CANVDS_TOKEN            an explicit endpoint
  2. <CANVDS_HOME>/CanVDS_rpc.json        written by a running CanVDS window (设置 → 允许 agent 连接) or `python -m zdiag.daemon`
  3. a headless daemon it starts itself    (set CANVDS_AUTOSTART=0 to forbid that)
Environment: CANVDS_HOME (folder with scan_data/, default: this folder),
CANVDS_PYTHON (interpreter for the auto-started daemon: the 32-bit Python that can load zlgcan.dll)."""
import asyncio
import atexit
import functools
import json
import os
import subprocess
import sys
import time
from typing import Any, Dict, List, Optional

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.environ.setdefault("CANVDS_HOME", HERE)

from zdiag import funcapi                         # noqa: E402
from zdiag.funcapi import FuncError                         # noqa: E402
from zdiag.paths import home_dir                            # noqa: E402
from zdiag.rpc import RpcError, ping, read_endpoint, rpc_call   # noqa: E402

try:
    from mcp.server.mcpserver import MCPServer             # mcp >= 2
except ImportError:                                        # mcp 1.x
    from mcp.server.fastmcp import FastMCP as MCPServer    # type: ignore

mcp = MCPServer("canvds", instructions=(
    "Operate the CanVDS diagnostic host. Typical order: get_status → device_open (模拟ECU(测试) for a dry run) → channel_start → "
    "uds_request / scan_module / send_frame / monitor_* . For multi-step jobs use 功能 (get_function_format, save_function, "
    "start_function). Tools act on the real device / bus (the simulator device is for dry runs). Do what the user asks directly; only ask when a required "
    "parameter is missing - for flashing: files, key DLL (global setting security.key_dll), CRC algorithm and erase range must come from the user, not be guessed."))

_daemon: Optional[subprocess.Popen] = None


# --------------------------------------------------------------------------- connection to CanVDS
def _home() -> str:
    return home_dir()


def _endpoint() -> Dict[str, str]:
    global _daemon
    if os.environ.get("CANVDS_URL") and os.environ.get("CANVDS_TOKEN"):
        return {"url": os.environ["CANVDS_URL"], "token": os.environ["CANVDS_TOKEN"]}
    e = read_endpoint(_home())
    if e and ping(e["url"]):
        return e
    if os.environ.get("CANVDS_AUTOSTART", "1") == "0":
        raise RpcError("没有运行中的 CanVDS 接口：请在 CanVDS 界面里开启「允许 agent 连接」，或运行 python -m zdiag.daemon")
    if _daemon is None or _daemon.poll() is not None:
        _daemon = subprocess.Popen([os.environ.get("CANVDS_PYTHON") or sys.executable, "-m", "zdiag.daemon", "--home", _home()],
                                   cwd=HERE, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
    for _ in range(150):
        e = read_endpoint(_home())
        if e and ping(e["url"]):
            return e
        if _daemon.poll() is not None:
            raise RpcError("自动启动 CanVDS daemon 失败: " + (_daemon.stderr.read().decode("utf-8", "replace")[-800:] or "未知原因"))
        time.sleep(0.1)
    raise RpcError("等待 CanVDS daemon 启动超时")


@atexit.register
def _stop_daemon() -> None:
    if _daemon is not None and _daemon.poll() is None:
        _daemon.terminate()
        try:
            _daemon.wait(10)
        except Exception:
            _daemon.kill()


def _call_sync(method: str, params: Dict[str, Any], timeout: float = 180.0) -> Dict[str, Any]:
    p = {k: v for k, v in params.items() if v is not None}
    try:
        e = _endpoint()
        return rpc_call(e["url"], e["token"], method, p, timeout)
    except RpcError as ex:
        return {"ok": False, "error": str(ex)}


async def call(method: str, **params: Any) -> Dict[str, Any]:
    timeout = 180.0 + float(params.get("seconds") or 0)
    return await asyncio.to_thread(_call_sync, method, params, timeout)


def _guard(fn):
    @functools.wraps(fn)                                   # keeps the signature the MCP schema is built from
    def w(*a, **k):
        try:
            return fn(*a, **k)
        except FuncError as e:
            return {"ok": False, "error": str(e)}
    return w


# --------------------------------------------------------------------------- modules / 功能: create from a recorded trace
@mcp.tool()
def get_function_format() -> str:
    """Describe what a 功能 (a module's list of steps: service / delay / unlock / driver / download / routine) is, with the typical flashing
    order and the workflow for turning a recorded trace into a function. Read this before creating a function."""
    return funcapi.FUNCTION_DOC


@mcp.tool()
@_guard
def analyze_trace(text: str = "", path: str = "", tester_id: str = "", ecu_id: str = "", time_unit: str = "auto",
                  delay_threshold_s: float = 0.5) -> Dict[str, Any]:
    """Read a recorded CAN trace (ZCANPro / CanVDS CSV export, Vector ASC, or lines like '7E2 02 10 03 ...'): reassembles ISO-TP, pairs the UDS
    requests with responses and drafts the steps of a 功能 (flashing included). Give the trace as `text` or as `path` of a file on this machine.
    The result has `exchanges` (a readable list), `pairs` (tester/ECU CAN IDs found), and `draft` = {steps, todo, warnings, notes}: `todo` lists
    what a trace cannot contain (flash files, key DLL, CRC algorithm) - ask the user for those, don't guess. tester_id / ecu_id (hex) select one
    ECU in a multi-ECU trace; time_unit auto | s | ms | us."""
    return funcapi.analyze_trace(text, path, tester_id, ecu_id, time_unit, delay_threshold_s)


@mcp.tool()
@_guard
def create_module(name: str, phys: str, resp: str = "", func: str = "") -> Dict[str, Any]:
    """Create a 软件模块 (an ECU with its diagnostic addresses; phys/resp/func are hex CAN IDs, resp empty = phys+8, func empty = 7DF)
    with the standard scan executors. Functions are then added with save_function."""
    return funcapi.create_module(name, phys, resp, func)


@mcp.tool()
@_guard
def check_function(steps: List[Dict[str, Any]], result: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Validate function steps without saving: missing files / key DLL, bad hex, bad ids... Returns ok and `problems`."""
    r = funcapi.check_steps(steps, result)
    return {"ok": r["ok"], "problems": r["problems"]}


@mcp.tool()
@_guard
def save_function(module: str, path: str, steps: List[Dict[str, Any]], result: Optional[Dict[str, Any]] = None, overwrite: bool = False,
                  allow_incomplete: bool = True) -> Dict[str, Any]:
    """Create / replace a 功能 of a module: path like '烧写' or '标定/零位标定' (folders are created). Steps that still lack files / key DLL are
    saved as a draft (allow_incomplete=true, the default; it cannot run until completed; set false to refuse such saves). The user sees it in 软件扫描 → 模块 → 功能."""
    return funcapi.save_function(module, path, steps, result, overwrite, allow_incomplete)


@mcp.tool()
@_guard
def read_function(module: str, path: str) -> Dict[str, Any]:
    """Read a saved 功能 (steps, result spec and what is still missing) e.g. to modify it."""
    return funcapi.read_function(module, path)


@mcp.tool()
@_guard
def delete_function(module: str, path: str) -> Dict[str, Any]:
    """Delete one saved 功能 (never a module)."""
    return funcapi.delete_function(module, path)


@mcp.tool()
async def start_function(channel: int, module: str, path: str) -> Dict[str, Any]:
    """Run a saved 功能 in the background (use this for flashing, which takes minutes); poll function_status. Real device: make sure the files / key DLL / CRC settings
    come from the user (a wrong one can brick the ECU)."""
    return await call("function_start", channel=channel, module=module, path=path)


@mcp.tool()
async def function_status() -> Dict[str, Any]:
    """State of the running / last 功能: per-step state and detail, progress, log lines, result."""
    return await call("function_status")


@mcp.tool()
async def stop_function() -> Dict[str, Any]:
    """Cancel the running 功能 (a flash cancelled midway leaves the ECU in its bootloader: tell the user)."""
    return await call("function_stop")


# --------------------------------------------------------------------------- device / channels
@mcp.tool()
async def get_status() -> Dict[str, Any]:
    """Current device, started channels (config, rx/tx/error counts, bus load), running cyclic senders, recording state."""
    return await call("status")


@mcp.tool()
async def device_list() -> Dict[str, Any]:
    """Supported devices (incl. the simulator '模拟ECU(测试)') with channel counts, and the current state."""
    return await call("device_list")


@mcp.tool()
async def device_open(name: str, index: int = 0) -> Dict[str, Any]:
    """Open a CAN device (a real one needs zlgcan.dll next to the program). Use the simulator for a dry run."""
    return await call("device_open", name=name, index=index)


@mcp.tool()
async def device_close() -> Dict[str, Any]:
    """Stop all channels and close the device."""
    return await call("device_close")


@mcp.tool()
async def channel_start(channel: int, protocol: str = "CAN", bitrate_kbps: float = 500.0, data_kbps: float = 2000.0,
                        fd_accel: bool = True, listen_only: bool = False, terminator: bool = True) -> Dict[str, Any]:
    """Start a channel. protocol "CAN" | "CAN FD"; fd_accel=false runs the whole CAN FD frame at the arbitration rate (no BRS)."""
    return await call("channel_start", channel=channel, protocol=protocol, bitrate_kbps=bitrate_kbps, data_kbps=data_kbps,
                      fd_accel=fd_accel, listen_only=listen_only, terminator=terminator)


@mcp.tool()
async def channel_stop(channel: int) -> Dict[str, Any]:
    """Stop a channel (also stops its cyclic senders)."""
    return await call("channel_stop", channel=channel)


# --------------------------------------------------------------------------- LIN (UDS over LIN, oil pump scan)
@mcp.tool()
async def lin_open(kind: str = "sim", channel: int = 0, baud: int = 19200, device: str = "", dll: str = "") -> Dict[str, Any]:
    """Open the LIN channel. kind='sim' = simulated oil pump (NAD 21) for a dry run; kind='zlg' = the ZLG box's LIN channel
    (the low-level binding still needs the SDK's LIN definitions and reports that). baud default 19200."""
    return await call("lin_open", kind=kind, channel=channel, baud=baud, device=device, dll=dll)


@mcp.tool()
async def lin_close() -> Dict[str, Any]:
    """Close the LIN channel."""
    return await call("lin_close")


@mcp.tool()
async def lin_request(nad: str, pdu: str, parse: str = "hex", p2_ms: int = 300) -> Dict[str, Any]:
    """One UDS request over LIN to the node with NAD `nad` (hex, e.g. '21'), PDU hex such as '22 F1 87'
    (transport: master request 0x3C / slave response 0x3D, multi-frame supported). Returns ok, data, value or nrc / error."""
    return await call("lin_request", nad=nad, pdu=pdu, parse=parse, p2_ms=p2_ms)


@mcp.tool()
async def lin_scan_nodes(first: str = "01", last: str = "7D", p2_ms: int = 50) -> Dict[str, Any]:
    """Find the LIN nodes that answer UDS: TesterPresent to every NAD in [first, last] (hex)."""
    return await call("lin_scan_nodes", first=first, last=last, p2_ms=p2_ms)


@mcp.tool()
async def lin_scan_pump(nad: str, dids: Optional[List[Any]] = None, session: bool = True, p2_ms: int = 300) -> Dict[str, Any]:
    """Read the oil pump's information from node `nad` (hex). Default: the standard identification DIDs (F187 part number, F189/F195
    software, F18A supplier, F18C serial, F191/F193 hardware). For supplier-specific data pass dids as ['F190', {'did':'F40C',
    'label':'转速','parse':'dec'}] - the DIDs of the pump come from its supplier, ask the user when unknown."""
    return await call("lin_scan_pump", nad=nad, dids=dids, session=session, p2_ms=p2_ms)


# --------------------------------------------------------------------------- CAN file transfer
@mcp.tool()
async def ft_send(channels: List[int], paths: List[str], base_id: str = "6A0", chunk_bytes: int = 1024, window: int = 128,
                  fd_frame_len: int = 64, extended: bool = False, link_timeout_ms: int = 600, idle_timeout_s: int = 30) -> Dict[str, Any]:
    """Send files and/or whole folders over CAN / CAN FD to a peer running ft_receive (any file type, background job, poll ft_status).
    channels: 1 or 2 started channel numbers, e.g. [0, 1]; with two, the file is split between them and if one goes silent the other
    carries on (and the lost one is used again when it returns). CAN FD channels use long frames automatically (fd_frame_len 64).
    An interrupted transfer resumes where it stopped when the same paths are sent again. base_id (hex) = first of 3 consecutive CAN IDs;
    base_id / extended / fd_frame_len / chunk_bytes should be identical on both ends."""
    return await call("ft_send", channels=channels, paths=paths, base_id=base_id, chunk_bytes=chunk_bytes, window=window,
                      fd_frame_len=fd_frame_len, extended=extended, link_timeout_ms=link_timeout_ms, idle_timeout_s=idle_timeout_s)


@mcp.tool()
async def ft_receive(channels: List[int], dest: str, base_id: str = "6A0", overwrite: bool = True, once: bool = True,
                     chunk_bytes: int = 1024, window: int = 128, fd_frame_len: int = 64, extended: bool = False) -> Dict[str, Any]:
    """Receive files / folders sent by ft_send into folder `dest` (background job, poll ft_status). Start it BEFORE the sender.
    once=True stops after one sender finished; False keeps listening until ft_stop. Unfinished files stay as <name>.cftpart (+ .cftmeta)
    and are continued by the next transfer of the same file. overwrite=False keeps an existing different file."""
    return await call("ft_receive", channels=channels, dest=dest, base_id=base_id, overwrite=overwrite, once=once,
                      chunk_bytes=chunk_bytes, window=window, fd_frame_len=fd_frame_len, extended=extended)


@mcp.tool()
async def ft_status() -> Dict[str, Any]:
    """Progress of the file transfers: state, current file, bytes done / total, speed, per-link state (alive, frames, errors), log."""
    return await call("ft_status")


@mcp.tool()
async def ft_stop(role: str = "") -> Dict[str, Any]:
    """Stop the file transfer(s) ('send', 'receive' or '' = all). Partial data is kept for resuming."""
    return await call("ft_stop", role=role)


# --------------------------------------------------------------------------- diagnostics
@mcp.tool()
async def uds_request(channel: int, pdu: str, module: str = "", phys: str = "", resp: str = "", func: str = "",
                      extended: Optional[bool] = None, addr: str = "phys", suppress: bool = False, parse: str = "hex") -> Dict[str, Any]:
    """Send one UDS request (hex PDU such as '22 F1 90') and return the response: ok, data, value (payload after the echoed
    header, parse = hex | ascii | dec), or nrc / nrc_text / error. Also returns the request/response log lines.
    Addressing: module = a 软件模块 name from list_modules (uses its addresses); or phys/resp/func = hex CAN IDs;
    default = the global diag settings."""
    return await call("uds_request", channel=channel, pdu=pdu, module=module or None, phys=phys or None, resp=resp or None,
                      func=func or None, extended=extended, addr=addr, suppress=suppress or None, parse=parse)


@mcp.tool()
async def read_did(channel: int, did: str, module: str = "", parse: str = "hex") -> Dict[str, Any]:
    """Read a data identifier (22), e.g. did='F190' with parse='ascii' for the VIN."""
    return await call("did_read", channel=channel, did=did, module=module or None, parse=parse)


@mcp.tool()
async def write_did(channel: int, did: str, data: str, module: str = "") -> Dict[str, Any]:
    """Write a data identifier (2E) with hex data. Changes the ECU."""
    return await call("did_write", channel=channel, did=did, data=data, module=module or None)


@mcp.tool()
async def read_dtc(channel: int, module: str = "", mask: str = "08") -> Dict[str, Any]:
    """Read DTCs by status mask (19 02): returns the list `dtcs` and `count`."""
    return await call("dtc_read", channel=channel, module=module or None, mask=mask)


@mcp.tool()
async def clear_dtc(channel: int, module: str = "", group: str = "FF FF FF") -> Dict[str, Any]:
    """Clear DTCs (14). Changes the ECU."""
    return await call("dtc_clear", channel=channel, module=module or None, group=group)


@mcp.tool()
async def ecu_reset(channel: int, module: str = "", kind: str = "01") -> Dict[str, Any]:
    """ECU reset (11). Restarts the ECU."""
    return await call("ecu_reset", channel=channel, module=module or None, kind=kind)


@mcp.tool()
async def session_control(channel: int, session: str = "03", module: str = "") -> Dict[str, Any]:
    """Diagnostic session control (10): 01 default, 02 programming, 03 extended."""
    return await call("session_control", channel=channel, session=session, module=module or None)


@mcp.tool()
async def security_unlock(channel: int, level: int = 1, module: str = "", dll: str = "") -> Dict[str, Any]:
    """Security access (27 seed/key). Uses the global key DLL (set once with set_settings section='security' values={'key_dll': path});
    `dll` overrides it for this call only ('builtin:xor' = demo algorithm)."""
    return await call("security_unlock", channel=channel, dll=dll or None, level=level, module=module or None)


@mcp.tool()
async def keepalive_start(channel: int, module: str = "", period_ms: int = 2000, addr: str = "func", suppress: bool = True) -> Dict[str, Any]:
    """Keep sending Tester Present (3E) until keepalive_stop."""
    return await call("keepalive_start", channel=channel, module=module or None, period_ms=period_ms, addr=addr, suppress=suppress)


@mcp.tool()
async def keepalive_stop(channel: int = -1) -> Dict[str, Any]:
    """Stop Tester Present (all channels when channel=-1)."""
    return await call("keepalive_stop", channel=channel)


@mcp.tool()
async def list_modules() -> Dict[str, Any]:
    """The 软件模块 of this machine (name, addresses, saved functions) and the scan fields."""
    return await call("modules")


@mcp.tool()
async def scan_module(channel: int, module: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
    """软件扫描 of one module: code (软件编码), version, date (硬件版本), dtc (故障码数量). fields = subset, default all."""
    return await call("scan", channel=channel, module=module, fields=fields)


@mcp.tool()
async def run_function(channel: int, module: str, path: str) -> Dict[str, Any]:
    """Run a saved 功能 of a module (see list_modules), e.g. path='故障检测'. May change the ECU (e.g. '清除故障')."""
    return await call("function_run", channel=channel, module=module, path=path)


# --------------------------------------------------------------------------- raw frames / monitor
@mcp.tool()
async def send_frame(channel: int, can_id: str, data: str = "", extended: bool = False, fd: bool = False, remote: bool = False,
                     count: int = 1, interval_ms: int = 0) -> Dict[str, Any]:
    """Send a raw CAN / CAN FD frame `count` times (blocking, at most 120 s). can_id and data are hex. Acts on the real bus."""
    return await call("frame_send", channel=channel, can_id=can_id, data=data, extended=extended, fd=fd, remote=remote,
                      count=count, interval_ms=interval_ms)


@mcp.tool()
async def cyclic_start(channel: int, key: str, can_id: str, data: str = "", period_ms: int = 100, extended: bool = False,
                       fd: bool = False, count: int = -1) -> Dict[str, Any]:
    """Send a frame every period_ms in the background; count -1 = until cyclic_stop. `key` names it. Acts on the real bus."""
    return await call("cyclic_start", channel=channel, key=key, can_id=can_id, data=data, period_ms=period_ms, extended=extended,
                      fd=fd, count=count)


@mcp.tool()
async def cyclic_stop(key: str = "") -> Dict[str, Any]:
    """Stop one cyclic sender (or all when key is empty); returns how many frames were sent."""
    return await call("cyclic_stop", key=key)


@mcp.tool()
async def monitor_trace(count: int = 100, ids: str = "", channel: int = -1, direction: str = "") -> Dict[str, Any]:
    """The last `count` frames seen on the bus (TX and RX, chronological). ids = hex IDs separated by space; direction TX | RX."""
    return await call("monitor_trace", count=count, ids=ids, channel=channel, direction=direction)


@mcp.tool()
async def monitor_snapshot(ids: str = "", channel: int = -1) -> Dict[str, Any]:
    """One row per (channel, direction, ID): last data, frame count and period in ms."""
    return await call("monitor_snapshot", ids=ids, channel=channel)


@mcp.tool()
async def monitor_capture(seconds: float = 1.0, ids: str = "", channel: int = -1, direction: str = "", limit: int = 500) -> Dict[str, Any]:
    """Wait `seconds` (<= 60) and return the frames that arrived in that time."""
    return await call("monitor_capture", seconds=seconds, ids=ids, channel=channel, direction=direction, limit=limit)


@mcp.tool()
async def monitor_clear() -> Dict[str, Any]:
    """Clear the monitor data."""
    return await call("monitor_clear")


@mcp.tool()
async def record_start(name: str = "") -> Dict[str, Any]:
    """Start saving every frame to <log folder>/<name>.csv in real time."""
    return await call("record_start", name=name)


@mcp.tool()
async def record_stop() -> Dict[str, Any]:
    """Stop the real-time recording."""
    return await call("record_stop")


# --------------------------------------------------------------------------- settings
@mcp.tool()
async def get_settings(section: str = "") -> Dict[str, Any]:
    """Global settings: sections diag (addr, keepalive, settings = timing / padding / ...), security (serial DID / file / id_map),
    log (folder / size / keep days), channel (fd_dl, zlgcan_dll, sim_profile). Empty section = all."""
    return await call("settings_get", section=section)


@mcp.tool()
async def set_settings(section: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """Change global settings (validated, saved, shared with the CanVDS windows). diag: {"addr": {"phys": "7E3", "resp": "7EB"},
    "settings": {"p2_ms": 500}, "keepalive": {...}}; security / log / channel: flat fields."""
    return await call("settings_set", section=section, values=values)


if __name__ == "__main__":
    mcp.run()
