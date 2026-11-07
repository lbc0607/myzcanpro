"""流程编排: data model, conditions and validation (no Qt, no bus).

A flow is a tree of nodes (plain dicts, so the JSON file is the model):

    准备(setup)      sequential, runs first: 打开设备 / 配置并启动通道
    泳道(lane)       one per channel, all lanes run in parallel
        扫描 scan, 功能 function, UDS请求 uds, 延时 delay, 周期发送 send / 停止发送 stop_send,
        条件 if (then / else), 循环 loop (body), 备注 note

Flow files reference the modules / functions of the local scan_data folder by name.
"""
import copy
import json
import os
import re
from dataclasses import dataclass, field
from typing import Callable, Dict, Iterator, List, Optional, Tuple

from .project import parse_hex, parse_id
from .scanlib import INFO_FIELDS, ScanStore

FORMAT, VERSION = "canvds-flow", 1

# type -> (label, container?)   container children keys: lane/setup "children", if "then"/"else", loop "body"
TYPE_LABEL = {
    "setup": "准备", "lane": "通道泳道", "open_device": "打开设备", "start_channel": "配置并启动通道",
    "scan": "软件扫描", "function": "执行功能", "uds": "UDS请求", "delay": "延时",
    "send": "周期发送报文", "stop_send": "停止周期发送", "if": "条件分支", "loop": "循环", "note": "备注",
}
CONTAINER_KEYS = {"setup": [("children", "")], "lane": [("children", "")],
                  "if": [("then", "成立时"), ("else", "否则")], "loop": [("body", "循环体")]}
SETUP_TYPES = ("open_device", "start_channel", "delay", "note")
LANE_TYPES = ("scan", "function", "uds", "delay", "send", "stop_send", "if", "loop", "note")
OPS = [("ok", "执行成功"), ("failed", "执行失败"), ("empty", "为空"), ("nonempty", "不为空"), ("eq", "等于"),
       ("ne", "不等于"), ("contains", "包含"), ("gt", "大于"), ("ge", "大于等于"), ("lt", "小于"), ("le", "小于等于")]
NEEDS_VALUE = ("eq", "ne", "contains", "gt", "ge", "lt", "le")
LOOP_MODES = [("count", "固定次数"), ("while", "条件成立时重复"), ("until", "重复直到条件成立")]
MAX_LOOP, MAX_DELAY_S, MAX_CHANNEL = 10000, 86400, 7


class FlowError(Exception):
    pass


# --------------------------------------------------------------------------- results
@dataclass
class FieldRes:
    text: str = ""
    raw: bytes = b""
    ok: bool = True


@dataclass
class NodeResult:
    ok: bool = True
    text: str = ""
    raw: bytes = b""
    fields: Dict[str, FieldRes] = field(default_factory=dict)
    error: str = ""
    skipped: bool = False


def to_number(text: str, raw: bytes = b"") -> Optional[float]:
    """Numeric value of a result: the raw bytes (big-endian) when there are any, else the text (decimal / 0x..)."""
    if raw:
        return float(int.from_bytes(raw, "big"))
    t = (text or "").strip()
    try:
        return float(int(t, 0))
    except ValueError:
        try:
            return float(t)
        except ValueError:
            return None


def _norm(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip()).upper()


def eval_cond(cond: dict, results: Dict[str, NodeResult]) -> Tuple[bool, str]:
    """(truth, human readable reason).  Raises FlowError when the referenced step has not run / cannot be compared."""
    ref, fld, op, val = cond.get("ref", ""), cond.get("field", ""), cond.get("op", "ok"), str(cond.get("value", ""))
    r = results.get(ref)
    if r is None:
        raise FlowError(f"条件引用的步骤「{ref}」还没有执行结果")
    if r.skipped:
        raise FlowError("条件引用的步骤被跳过了，没有结果")
    if fld:
        fr = r.fields.get(fld)
        if fr is None:
            raise FlowError(f"步骤「{ref}」没有字段「{fld}」")
        text, raw, ok = fr.text, fr.raw, fr.ok
    else:
        text, raw, ok = r.text, r.raw, r.ok
    shown = text if text else "(空)"
    if op == "ok":
        return ok, f"执行{'成功' if ok else '失败'}"
    if op == "failed":
        return (not ok), f"执行{'成功' if ok else '失败'}"
    empty = _norm(text) in ("", "-")
    if op == "empty":
        return empty, f"结果={shown}"
    if op == "nonempty":
        return (not empty), f"结果={shown}"
    if op == "contains":
        return _norm(val) in _norm(text), f"结果={shown} 包含 {val}?"
    a, b = to_number(text, raw), to_number(val)
    if op in ("eq", "ne"):
        if a is not None and b is not None:
            same = a == b
        else:
            same = _norm(text) == _norm(val)
        return (same if op == "eq" else not same), f"结果={shown} {'=' if op == 'eq' else '≠'} {val}?"
    if op in ("gt", "ge", "lt", "le"):
        if a is None or b is None:
            raise FlowError(f"无法按数值比较：结果「{shown}」与「{val}」")
        res = {"gt": a > b, "ge": a >= b, "lt": a < b, "le": a <= b}[op]
        sym = {"gt": ">", "ge": "≥", "lt": "<", "le": "≤"}[op]
        return res, f"结果={a:g} {sym} {b:g}?"
    raise FlowError(f"未知的比较方式 {op}")


# --------------------------------------------------------------------------- nodes
def _defaults(kind: str) -> dict:
    d = {
        "setup": {"name": "准备", "children": []},
        "lane": {"name": "通道0", "channel": 0, "children": []},
        "open_device": {"name": "打开设备", "device": "USBCANFD-200U", "index": 0},
        "start_channel": {"name": "启动通道", "channel": 0, "cfg": {"protocol": "CAN", "bitrate_kbps": 500.0, "data_kbps": 2000.0,
                                                                  "fd_accel": True, "listen_only": False, "terminator": True}},
        "scan": {"name": "软件扫描", "module": "", "fields": [k for k, _ in INFO_FIELDS]},
        "function": {"name": "执行功能", "module": "", "path": ""},
        "uds": {"name": "UDS请求", "module": "", "pdu": "11 01", "pre_session": False, "skip": -1, "parse": "hex"},
        "delay": {"name": "延时", "seconds": 3.0},
        "send": {"name": "周期发送", "key": "cyc1", "can_id": "123", "extended": False, "fd": False, "data": "00 00 00 00 00 00 00 00",
                 "period_ms": 100},
        "stop_send": {"name": "停止周期发送", "key": ""},
        "if": {"name": "条件分支", "cond": {"ref": "", "field": "", "op": "ok", "value": ""}, "then": [], "else": []},
        "loop": {"name": "循环", "mode": "count", "count": 2, "max_iter": 100,
                 "cond": {"ref": "", "field": "", "op": "ok", "value": ""}, "body": []},
        "note": {"name": "备注", "text": ""},
    }
    return copy.deepcopy(d[kind])


class Flow:
    def __init__(self, name: str = "新流程") -> None:
        self.name = name
        self.nodes: List[dict] = []
        self._n = 0

    # -- construction
    def new_id(self) -> str:
        self._n += 1
        return f"n{self._n}"

    def make(self, kind: str, **params) -> dict:
        if kind not in TYPE_LABEL:
            raise FlowError(f"未知节点类型 {kind}")
        n = {"id": self.new_id(), "type": kind, "enabled": True, "on_fail": "stop"}
        n.update(_defaults(kind))
        n.update(params)
        return n

    def adopt(self, node: dict) -> dict:
        """Give a (possibly copied) node and its descendants fresh ids."""
        for n in walk([node]):
            n["id"] = self.new_id()
        return node

    # -- navigation
    def walk(self) -> Iterator[dict]:
        return walk(self.nodes)

    def find(self, node_id: str) -> Optional[dict]:
        for n in self.walk():
            if n["id"] == node_id:
                return n
        return None

    def locate(self, node_id: str) -> Optional[Tuple[list, int]]:
        """(the list holding the node, its index)."""
        for lst in self._lists():
            for i, n in enumerate(lst):
                if n["id"] == node_id:
                    return lst, i
        return None

    def _lists(self) -> Iterator[list]:
        yield self.nodes
        for n in walk(self.nodes):
            for key, _ in CONTAINER_KEYS.get(n["type"], []):
                yield n.setdefault(key, [])

    def lanes(self) -> List[dict]:
        return [n for n in self.nodes if n["type"] == "lane"]

    def setup(self) -> Optional[dict]:
        for n in self.nodes:
            if n["type"] == "setup":
                return n
        return None

    def ensure_setup(self) -> dict:
        s = self.setup()
        if s is None:
            s = self.make("setup")
            self.nodes.insert(0, s)
        return s

    # -- persistence
    def to_dict(self) -> dict:
        return {"format": FORMAT, "version": VERSION, "name": self.name, "nodes": self.nodes}

    @staticmethod
    def from_dict(d: dict) -> "Flow":
        if not isinstance(d, dict) or d.get("format") != FORMAT:
            raise FlowError("不是 CanVDS 流程文件")
        f = Flow(str(d.get("name", "流程")))
        f.nodes = copy.deepcopy(d.get("nodes", []))
        ids = []
        for n in walk(f.nodes):
            if n.get("type") not in TYPE_LABEL:
                raise FlowError(f"流程里有未知的节点类型: {n.get('type')}")
            n.setdefault("enabled", True)
            n.setdefault("on_fail", "stop")
            for k, v in _defaults(n["type"]).items():
                n.setdefault(k, v)
            ids.append(str(n.get("id", "")))
        used = {i for i in ids if i}
        f._n = max([int(i[1:]) for i in used if re.fullmatch(r"n\d+", i)] + [0])
        seen = set()
        for n in walk(f.nodes):                                      # repair missing / duplicated ids
            if not n.get("id") or n["id"] in seen:
                n["id"] = f.new_id()
            seen.add(n["id"])
        return f

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fp:
            json.dump(self.to_dict(), fp, ensure_ascii=False, indent=2)

    @staticmethod
    def load(path: str) -> "Flow":
        try:
            with open(path, "r", encoding="utf-8") as fp:
                return Flow.from_dict(json.load(fp))
        except (OSError, ValueError) as e:
            raise FlowError(f"读取流程文件失败: {e}")


def walk(nodes: List[dict]) -> Iterator[dict]:
    for n in nodes:
        yield n
        for key, _ in CONTAINER_KEYS.get(n.get("type"), []):
            yield from walk(n.get(key) or [])


def children_of(node: dict) -> List[Tuple[str, str, list]]:
    """[(key, label, list)] of a container node."""
    return [(k, lab, node.setdefault(k, [])) for k, lab in CONTAINER_KEYS.get(node["type"], [])]


def describe(n: dict) -> str:
    """One-line parameter summary for the editor list."""
    t = n["type"]
    if t == "lane":
        return f"通道{n.get('channel', 0)}"
    if t == "open_device":
        return f"{n.get('device')}  索引{n.get('index', 0)}"
    if t == "start_channel":
        c = n.get("cfg", {})
        s = f"通道{n.get('channel', 0)}  {c.get('protocol', 'CAN')} {c.get('bitrate_kbps', 500):g}k"
        if c.get("protocol") == "CAN FD":
            s += f"/{c.get('data_kbps', 2000):g}k" if c.get("fd_accel", True) else " 不加速"
        return s + ("  只听" if c.get("listen_only") else "")
    if t == "scan":
        lab = dict(INFO_FIELDS)
        return f"{n.get('module')}  [{' '.join(lab.get(k, k) for k in n.get('fields', []))}]"
    if t == "function":
        return f"{n.get('module')} › {n.get('path')}"
    if t == "uds":
        return f"{n.get('module')}  {n.get('pdu')}" + ("  (先进扩展会话)" if n.get("pre_session") else "")
    if t == "delay":
        return f"{n.get('seconds', 0):g} 秒"
    if t == "send":
        return f"ID {n.get('can_id')}  [{n.get('data')}]  每 {n.get('period_ms')} ms" + ("  FD" if n.get("fd") else "")
    if t == "stop_send":
        return n.get("key") or "全部"
    if t in ("if", "loop"):
        c = n.get("cond", {})
        cs = cond_text(c)
        if t == "if":
            return cs
        m = n.get("mode", "count")
        return f"{n.get('count', 1)} 次" if m == "count" else f"{dict(LOOP_MODES)[m]}: {cs} (最多{n.get('max_iter', 100)}次)"
    if t == "note":
        return n.get("text", "")
    return ""


def cond_text(c: dict) -> str:
    op = dict(OPS).get(c.get("op", "ok"), c.get("op"))
    f = f".{c['field']}" if c.get("field") else ""
    v = f" {c.get('value')}" if c.get("op") in NEEDS_VALUE else ""
    return f"[{c.get('ref') or '?'}{f}] {op}{v}"


# --------------------------------------------------------------------------- validation
def validate(flow: Flow, store: Optional[ScanStore] = None) -> List[str]:
    """Everything that can be checked without a bus.  Empty list = runnable."""
    from .sendlist import SendItem
    errs: List[str] = []
    ids = [n["id"] for n in flow.walk()]
    if len(ids) != len(set(ids)):
        errs.append("节点编号重复")
    mods = {m.name: m for m in store.modules()} if store else None
    top = [n["type"] for n in flow.nodes]
    if top.count("setup") > 1:
        errs.append("只能有一个「准备」")
    if any(t not in ("setup", "lane") for t in top):
        errs.append("最外层只能是「准备」和「通道泳道」")
    chans = [n.get("channel") for n in flow.lanes()]
    for c in chans:
        if not isinstance(c, int) or not 0 <= c <= MAX_CHANNEL:
            errs.append(f"泳道的通道号应为 0~{MAX_CHANNEL}")
    if len(chans) != len(set(chans)):
        errs.append("两条泳道使用了同一个通道（同一通道的操作请放在同一条泳道里）")
    if not flow.lanes():
        errs.append("没有泳道：请至少添加一条通道泳道")
    pos = {n["id"]: i for i, n in enumerate(flow.walk())}

    def check_cond(n: dict, c: dict) -> None:
        nm = f"「{n['name']}」"
        ref = flow.find(c.get("ref", ""))
        if ref is None:
            errs.append(f"{nm} 的条件没有选择有效的步骤")
            return
        if ref["type"] in ("setup", "lane", "if", "loop", "note"):
            errs.append(f"{nm} 的条件引用了不产生结果的步骤「{ref['name']}」")
        if c.get("field") and not (ref["type"] == "scan" and c["field"] in dict(INFO_FIELDS)):
            errs.append(f"{nm} 的条件字段「{c['field']}」无效")
        if c.get("op") not in dict(OPS):
            errs.append(f"{nm} 的比较方式无效")
        if c.get("op") in NEEDS_VALUE and str(c.get("value", "")).strip() == "":
            errs.append(f"{nm} 的条件缺少比较值")

    def check(n: dict, parent: str) -> None:
        t, nm = n["type"], f"「{n.get('name')}」"
        allowed = SETUP_TYPES if parent == "setup" else LANE_TYPES if parent in ("lane", "body", "then", "else") else None
        if t not in ("setup", "lane") and allowed is not None and t not in allowed:
            errs.append(f"{nm}（{TYPE_LABEL[t]}）不能放在{'准备' if parent == 'setup' else '泳道'}里")
        if t in ("scan", "function", "uds"):
            m = n.get("module", "")
            if mods is not None and m not in mods:
                errs.append(f"{nm} 的软件模块「{m}」在本机 scan_data 里不存在")
        if t == "scan":
            bad = [k for k in n.get("fields", []) if k not in dict(INFO_FIELDS)]
            if bad or not n.get("fields"):
                errs.append(f"{nm} 的扫描字段无效或为空")
        if t == "function" and store is not None and mods is not None and n.get("module") in mods:
            p = os.path.join(store.func_dir(mods[n["module"]]), *str(n.get("path", "")).replace("\\", "/").split("/")) + ".json"
            if not n.get("path") or not os.path.isfile(p):
                errs.append(f"{nm} 的功能「{n.get('path')}」在 {n.get('module')} 里不存在")
        if t == "uds":
            try:
                if not parse_hex(n.get("pdu", "")):
                    raise ValueError("空")
            except ValueError as e:
                errs.append(f"{nm} 的请求报文无效: {e}")
        if t == "delay" and not 0 <= float(n.get("seconds", 0)) <= MAX_DELAY_S:
            errs.append(f"{nm} 的延时应为 0~{MAX_DELAY_S} 秒")
        if t == "send":
            try:
                data = parse_hex(n.get("data", ""))
                fd = bool(n.get("fd"))
                from .sendlist import FD_LENGTHS
                length = len(data) if not fd else next((x for x in FD_LENGTHS if x >= len(data)), 64)
                SendItem(can_id=parse_id(str(n.get("can_id", ""))), extended=bool(n.get("extended")), length=length, data=data,
                         count=-1, interval_ms=int(n.get("period_ms", 0))).validate(fd)
                if int(n.get("period_ms", 0)) < 1:
                    raise ValueError("周期应 ≥ 1 ms")
                if not str(n.get("key", "")).strip():
                    raise ValueError("缺少名称(用于停止)")
            except (ValueError, TypeError) as e:
                errs.append(f"{nm} 的周期发送参数无效: {e}")
        if t == "open_device" and not n.get("device"):
            errs.append(f"{nm} 没有选择设备")
        if t == "start_channel":
            from .busstats import ChannelCfg
            try:
                if not isinstance(n.get("channel"), int) or not 0 <= n["channel"] <= MAX_CHANNEL:
                    raise ValueError(f"通道号应为 0~{MAX_CHANNEL}")
                ChannelCfg(**n.get("cfg", {})).validate()
            except (ValueError, TypeError) as e:
                errs.append(f"{nm} 的通道配置无效: {e}")
        if t == "loop":
            if n.get("mode") not in dict(LOOP_MODES):
                errs.append(f"{nm} 的循环方式无效")
            if n.get("mode") == "count" and not 1 <= int(n.get("count", 0)) <= MAX_LOOP:
                errs.append(f"{nm} 的循环次数应为 1~{MAX_LOOP}")
            if n.get("mode") != "count" and not 1 <= int(n.get("max_iter", 0)) <= MAX_LOOP:
                errs.append(f"{nm} 的最多循环次数应为 1~{MAX_LOOP}")
        if t == "if" or (t == "loop" and n.get("mode") != "count"):
            check_cond(n, n.get("cond", {}))
            ref = flow.find(n.get("cond", {}).get("ref", ""))
            if ref is not None and pos.get(ref["id"], 0) >= pos[n["id"]] and t == "if":
                errs.append(f"{nm} 的条件引用了排在它后面的步骤「{ref['name']}」")
        for key, _ in CONTAINER_KEYS.get(t, []):
            for c in n.get(key) or []:
                check(c, t if t in ("setup", "lane") else key)

    for n in flow.nodes:
        check(n, "")
    return errs
