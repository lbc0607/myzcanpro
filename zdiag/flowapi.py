"""Agent-facing API of 流程编排 (stdlib only, Python 3.8): format description, catalog of the local scan_data,
validate / save / load / outline and a headless run.  Used by `zdiag.cli` (any agent via the shell) and by
`mcp_server.py` (MCP).  Every function returns plain JSON-able data."""
import json
import os
import sys
import threading
from typing import Any, Dict, List, Optional, Union

from .flow import (FORMAT, LANE_TYPES, LOOP_MODES, MAX_CHANNEL, MAX_DELAY_S, MAX_LOOP, NEEDS_VALUE, OPS, SETUP_TYPES, TYPE_LABEL,
                   Flow, FlowError, children_of, describe, validate, walk)
from .project import SIM_DEVICE
from .project import DEVICES
from .scanlib import INFO_FIELDS, PARSE_MODES, ScanStore, function_paths

FORMAT_DOC = """\
CanVDS 流程文件格式 (JSON, format="canvds-flow")
================================================
{"format":"canvds-flow","version":1,"name":"流程名","nodes":[ 准备节点, 泳道节点, 泳道节点... ]}

结构
- 最外层只能放一个「准备」(type=setup，可省略) 和若干「泳道」(type=lane，每个通道一条)。
- 准备里依次执行，只能放: open_device / start_channel / delay / note。准备失败则不执行任何泳道。
- 各泳道同时(并行)执行；泳道内按顺序执行，只能放: scan / function / uds / delay / send / stop_send / if / loop / note。
- 两条泳道不能用同一个通道；同一通道的所有操作写在同一条泳道里。

每个节点都有: id(字符串，全流程唯一，如 "n1"/"scan1"，条件靠它引用), type, name(显示名),
enabled(默认true), on_fail("stop"=失败即中止本泳道(默认) | "continue"=继续)。容器节点的子节点列表见下。

节点类型与字段
- setup: children[]
- lane: channel(0~7), children[]
- open_device: device(设备名，见 catalog.devices；模拟测试用 "%(sim)s"), index(0)
- start_channel: channel, cfg{protocol:"CAN"|"CAN FD", bitrate_kbps:500, data_kbps:2000, fd_accel:true, listen_only:false, terminator:true}
- scan(软件扫描): module(模块名，必须在本机 scan_data 里), fields[] 取自 catalog.scan_fields 的 key (code/version/date/dtc)
- function(执行功能): module, path(模块「功能」文件夹下的相对路径，如 "故障检测" 或 "标定/零位标定"，见 catalog.modules[].functions)
- uds(UDS请求): module, pdu(十六进制, 如 "19 02 08" / "14 FF FF FF"), pre_session(false), skip(-1自动), parse("hex"|"ascii"|"dec")
- delay: seconds(0~%(maxd)s)
- send(周期发送报文，之后一直发送直到 stop_send 或泳道结束): key(名称，供停止用), can_id("321"十六进制), extended, fd, data("01 02 03 04"), period_ms(>=1)
- stop_send: key(留空=停止本泳道全部周期发送)
- if(条件分支): cond{...}, then[](成立时), else[](否则)
- loop(循环): mode("count"固定次数 | "while"条件成立时重复 | "until"重复直到条件成立), count(1~%(maxl)s), max_iter(1~%(maxl)s, while/until的保险上限), cond{...}, body[]
- note: text(备注，不执行)

条件 cond = {"ref":"被引用步骤的id", "field":"", "op":"...", "value":""}
- ref 必须是排在它前面、会产生结果的步骤(scan/function/uds/send...，不能是 if/loop/note/泳道)。
- field: 引用 scan 步骤时可填 code/version/date/dtc 取其中一个字段，否则留空。
- op: ok(执行成功) failed(执行失败) empty(为空) nonempty(不为空) eq ne contains gt ge lt le (后7个需要 value；数值按原始字节大端比较)。
- 例: 「没有故障码就跳过清除」= if 的 cond 为 {ref: 读故障码步骤id, field:"", op:"empty"} 且把清除放在 else，或 op "eq" value "0"(dtc字段)。

示例(精简): 准备(开设备+启动通道0) → 泳道0: 扫描 → 读故障码 → 若有故障码则清除 → 复位 → 延时3秒 → 再读
{"format":"canvds-flow","version":1,"name":"示例","nodes":[
 {"id":"s","type":"setup","name":"准备","children":[
   {"id":"o","type":"open_device","name":"打开设备","device":"%(sim)s","index":0},
   {"id":"c0","type":"start_channel","name":"启动通道","channel":0,"cfg":{"protocol":"CAN","bitrate_kbps":500}}]},
 {"id":"l0","type":"lane","name":"通道0","channel":0,"children":[
   {"id":"sc","type":"scan","name":"扫描","module":"<模块名>","fields":["code","version"]},
   {"id":"d1","type":"uds","name":"读故障码","module":"<模块名>","pdu":"19 02 08"},
   {"id":"if1","type":"if","name":"有故障码?","cond":{"ref":"d1","field":"","op":"nonempty"},
    "then":[{"id":"clr","type":"uds","name":"清除","module":"<模块名>","pdu":"14 FF FF FF"}],"else":[]},
   {"id":"w","type":"delay","name":"等待","seconds":3}]}]}

使用步骤: 1) get_catalog 看本机有哪些模块/功能 2) 写流程 3) validate_flow 校验并按错误修改 4) save_flow 保存 5) 用户在 CanVDS「流程编排」里打开，或 run_flow 直接执行。
注意: run_flow 使用 CanVDS 当前打开的设备（与界面共用，无界面时由后台 daemon 持有）；它会真实操作设备/总线，需要时可先用 simulate=true 在模拟ECU上试跑。
""" % {"sim": SIM_DEVICE, "maxd": MAX_DELAY_S, "maxl": MAX_LOOP}


# --------------------------------------------------------------------------- locations
def home_dir(home: Optional[str] = None) -> str:
    if home:
        return home
    if os.environ.get("CANVDS_HOME"):
        return os.environ["CANVDS_HOME"]
    from zdiag_ui.appctx import app_dir
    return app_dir()


def scan_dir(home: Optional[str] = None) -> str:
    return os.path.join(home_dir(home), "scan_data")


def flow_dir(home: Optional[str] = None) -> str:
    return os.environ.get("CANVDS_FLOW_DIR") or os.path.join(home_dir(home), "flows")


def _store(home: Optional[str]) -> ScanStore:
    s = ScanStore(scan_dir(home))
    s.ensure_defaults()
    return s


def flow_path(name: str, home: Optional[str] = None) -> str:
    """A flow name (no folders) inside the flow folder: an agent can never write elsewhere."""
    n = (name or "").strip()
    if n.lower().endswith(".json"):
        n = n[:-5]
    if not n or any(c in n for c in '\\/:*?"<>|') or n in (".", "..") or ".." in n:
        raise FlowError(f"流程名无效: {name!r}（不能为空，不能含 \\ / : * ? \" < > | ..）")
    return os.path.join(flow_dir(home), n + ".json")


# --------------------------------------------------------------------------- catalog
def catalog(home: Optional[str] = None) -> Dict[str, Any]:
    store = _store(home)
    mods = []
    for m in store.modules():
        mods.append({"name": m.name, "functions": function_paths(store, m.name)})
    return {
        "home": home_dir(home), "scan_dir": store.root, "flow_dir": flow_dir(home),
        "modules": mods,
        "scan_fields": [{"key": k, "label": v} for k, v in INFO_FIELDS],
        "devices": [SIM_DEVICE] + [d for d in DEVICES if d != SIM_DEVICE],
        "node_types": {"准备内": list(SETUP_TYPES), "泳道内": list(LANE_TYPES), "label": dict(TYPE_LABEL)},
        "ops": [{"op": k, "label": v, "needs_value": k in NEEDS_VALUE} for k, v in OPS],
        "loop_modes": [{"mode": k, "label": v} for k, v in LOOP_MODES],
        "parse_modes": [{"mode": k, "label": v} for k, v in PARSE_MODES],
        "limits": {"channel": [0, MAX_CHANNEL], "delay_s": MAX_DELAY_S, "loop": MAX_LOOP},
    }


# --------------------------------------------------------------------------- flows
def to_flow(obj: Union[dict, str]) -> Flow:
    """A flow from a dict / JSON text; the envelope (format/version/name) may be left out."""
    if isinstance(obj, str):
        try:
            obj = json.loads(obj)
        except ValueError as e:
            raise FlowError(f"不是有效的 JSON: {e}")
    if isinstance(obj, list):
        obj = {"nodes": obj}
    if not isinstance(obj, dict):
        raise FlowError("流程应为 JSON 对象")
    d = dict(obj)
    d.setdefault("format", FORMAT)
    d.setdefault("name", "流程")
    nodes = d.get("nodes")
    if not isinstance(nodes, list):
        raise FlowError("缺少 nodes 列表")
    for n in walk(nodes):
        if not isinstance(n, dict) or "type" not in n:
            raise FlowError("每个节点都需要 type 字段")
        n.setdefault("name", TYPE_LABEL.get(n["type"], n["type"]))
    return Flow.from_dict(d)


def outline(flow: Flow) -> str:
    lines: List[str] = []

    def rec(nodes: List[dict], depth: int) -> None:
        for n in nodes:
            tag = "" if n.get("enabled", True) else " (已禁用)"
            try:
                desc = describe(n)
            except (ValueError, TypeError, KeyError, AttributeError):
                desc = "(参数格式有误)"
            lines.append(f"{'  ' * depth}[{n['id']}] {TYPE_LABEL[n['type']]}: {n['name']}{tag}  {desc}".rstrip())
            for key, label, lst in children_of(n) if n["type"] in ("setup", "lane", "if", "loop") else []:
                if label:
                    lines.append(f"{'  ' * (depth + 1)}↳ {label}" + ("" if lst else "（空）"))
                rec(lst, depth + (2 if label else 1))
    rec(flow.nodes, 0)
    return "\n".join(lines)


def check(obj: Union[dict, str], home: Optional[str] = None) -> Dict[str, Any]:
    try:
        flow = to_flow(obj)
    except FlowError as e:
        return {"ok": False, "errors": [str(e)], "outline": ""}
    try:
        errs = validate(flow, _store(home))
    except (ValueError, TypeError, KeyError, AttributeError) as e:
        errs = [f"流程里有字段类型不对，无法校验: {type(e).__name__}: {e}"]
    return {"ok": not errs, "errors": errs, "outline": outline(flow) if not errs or flow.nodes else ""}


def save(obj: Union[dict, str], name: str, home: Optional[str] = None, overwrite: bool = False,
         allow_invalid: bool = False) -> Dict[str, Any]:
    chk = check(obj, home)
    if not chk["ok"] and not allow_invalid:
        return {"saved": False, "reason": "流程校验未通过，未保存（草稿可加 allow_invalid）", **chk}
    path = flow_path(name, home)
    if os.path.exists(path) and not overwrite:
        return {"saved": False, "reason": f"已存在同名流程 {os.path.basename(path)}，如要覆盖请设置 overwrite=true", **chk}
    flow = to_flow(obj)
    flow.name = os.path.splitext(os.path.basename(path))[0]
    flow.save(path)
    return {"saved": True, "path": path, **chk}


def load(name: str, home: Optional[str] = None) -> Dict[str, Any]:
    path = flow_path(name, home)
    flow = Flow.load(path)
    return {"path": path, "flow": flow.to_dict(), "outline": outline(flow)}


def list_flows(home: Optional[str] = None) -> List[Dict[str, Any]]:
    d = flow_dir(home)
    out = []
    if os.path.isdir(d):
        for f in sorted(os.listdir(d)):
            if f.lower().endswith(".json"):
                try:
                    fl = Flow.load(os.path.join(d, f))
                    out.append({"name": f[:-5], "lanes": len(fl.lanes()), "steps": len(list(fl.walk()))})
                except FlowError:
                    out.append({"name": f[:-5], "error": "不是有效的流程文件"})
    return out


# --------------------------------------------------------------------------- headless run
def run(obj: Union[dict, str], home: Optional[str] = None, timeout: float = 300.0, simulate: bool = False) -> Dict[str, Any]:
    """Execute a flow without the GUI.  Blocks until it ends or `timeout` seconds pass (then it is cancelled)."""
    from .flowrun import CtxEnv, FlowRunner
    chk = check(obj, home)
    if not chk["ok"]:
        return {"ok": False, "message": "流程校验未通过，未执行", "steps": [], **chk}
    flow = to_flow(obj)
    if simulate:
        for n in flow.walk():
            if n["type"] == "open_device":
                n["device"] = SIM_DEVICE
    from zdiag_ui.appctx import AppContext
    ctx = AppContext(home_dir(home))
    env = CtxEnv(ctx)
    cancel, events = threading.Event(), []
    timer = threading.Timer(max(1.0, float(timeout)), cancel.set)
    timer.daemon = True
    timer.start()
    try:
        rep = FlowRunner(flow, env, cancel, on_event=events.append).run()
    finally:
        timer.cancel()
        try:
            ctx.dev.shutdown()
        except Exception:
            pass
    last: Dict[str, Any] = {}
    for e in events:
        if e.state == "reset":
            last.pop(e.node, None)
        else:
            last[e.node] = e
    steps = []
    for n in flow.walk():
        e = last.get(n["id"])
        if e is not None:
            steps.append({"id": n["id"], "name": n["name"], "type": n["type"], "state": e.state, "text": (e.text or "")[:400]})
    timed_out = cancel.is_set() and not rep.ok and rep.message == "已停止"
    return {"ok": bool(rep.ok), "message": "超时，已停止" if timed_out else rep.message, "simulated": simulate, "steps": steps,
            "failed": [s for s in steps if s["state"] == "fail"]}
