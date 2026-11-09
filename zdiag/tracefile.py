"""Read a recorded CAN trace (ZCANPro / CanVDS CSV export, Vector ASC, or plain "ID data" lines), reassemble ISO-TP and
pair the UDS requests with their responses.  Stdlib only (Python 3.8).

    frames, info = parse_frames(text)           # tolerant, header-driven for CSV / tab exports
    msgs = reassemble(frames)                    # ISO-TP SF / FF / CF -> whole messages (flow control frames are skipped)
    exchanges, info2 = pair(msgs)                # request / response pairs; only IDs that really talk UDS are kept"""
import re
from dataclasses import dataclass
from typing import Any, Dict, List, Optional, Tuple

REQUEST_SIDS = {0x10, 0x11, 0x14, 0x19, 0x22, 0x23, 0x24, 0x27, 0x28, 0x2A, 0x2C, 0x2E, 0x2F, 0x31, 0x34, 0x35, 0x36, 0x37,
                0x38, 0x3D, 0x3E, 0x83, 0x84, 0x85, 0x86, 0x87}
SERVICE_NAME = {0x10: "会话控制", 0x11: "ECU复位", 0x14: "清除故障", 0x19: "读故障码", 0x22: "读数据", 0x27: "安全访问", 0x28: "通信控制",
                0x2E: "写数据", 0x2F: "IO控制", 0x31: "例程控制", 0x34: "请求下载", 0x36: "传输数据", 0x37: "退出传输", 0x3E: "TesterPresent",
                0x85: "DTC设置", 0x83: "时序参数"}
FUNCTIONAL_IDS = {0x7DF}
MAX_PAIR_DELAY_S = 30.0


@dataclass
class Frame:
    ts: float
    can_id: int
    data: bytes
    ch: int = 0
    dir: str = ""
    line: int = 0


@dataclass
class Message:
    ts: float                 # time of the last frame of the message
    can_id: int
    data: bytes               # ISO-TP payload
    ts_first: float = 0.0
    frames: int = 1


@dataclass
class Exchange:
    t_req: float
    t_resp: float
    req_id: int
    resp_id: int
    req: bytes
    resp: Optional[bytes]
    pending: int = 0          # how many "78 response pending" came first
    suppressed: bool = False  # no answer expected (suppress-positive-response bit)

    @property
    def sid(self) -> int:
        return self.req[0]

    @property
    def nrc(self) -> Optional[int]:
        return self.resp[2] if self.resp and self.resp[0] == 0x7F and len(self.resp) >= 3 else None

    @property
    def ok(self) -> bool:
        return bool(self.resp) and self.resp[0] == self.req[0] + 0x40


# --------------------------------------------------------------------------- reading text
_HEX2 = re.compile(r"^(?:0x)?[0-9A-Fa-f]{2}$")
_NUM = re.compile(r"^[+-]?\d+(?:\.\d+)?$")
_ASC = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+(\d+)\s+([0-9A-Fa-f]+)x?\s+(Rx|Tx|RX|TX)\s+d\s+(\d+)((?:\s+[0-9A-Fa-f]{2})*)", re.I)
_ASC_FD = re.compile(r"^\s*(\d+(?:\.\d+)?)\s+CANFD\s+(\d+)\s+(Rx|Tx)\s+([0-9A-Fa-f]+)x?\s+\S*\s+\d\s+\d\s+[0-9A-Fa-f]+\s+(\d+)((?:\s+[0-9A-Fa-f]{2})*)", re.I)

COL_ID = ("帧id", "帧 id", "id", "identifier", "can id", "canid", "arbitration id")
COL_DATA = ("数据", "data", "payload", "数据(hex)")
COL_TIME = ("时间标识", "时间", "time", "timestamp", "时间戳")
COL_DIR = ("方向", "传输方向", "dir", "direction")
COL_CH = ("通道", "channel", "ch")


def _hex_bytes(tokens: List[str]) -> Optional[bytes]:
    """Data bytes from separated tokens ("02 10 03") or one contiguous string ("021003")."""
    toks = [t for t in tokens if t != ""]
    if not toks:
        return b""
    if all(_HEX2.match(t) for t in toks):
        return bytes(int(t[2:] if t.lower().startswith("0x") else t, 16) for t in toks)
    if len(toks) == 1:
        t = toks[0][2:] if toks[0].lower().startswith("0x") else toks[0]
        if len(t) % 2 == 0 and re.fullmatch(r"[0-9A-Fa-f]+", t):
            return bytes.fromhex(t)
    return None


def _parse_id(tok: str) -> Optional[int]:
    t = tok.strip().lower()
    if t.endswith("x") and not t.startswith("0x"):
        t = t[:-1]
    if t.startswith("0x"):
        t = t[2:]
    return int(t, 16) if re.fullmatch(r"[0-9a-f]{1,8}", t) else None


def _parse_time(tok: str) -> Optional[float]:
    t = tok.strip()
    m = re.fullmatch(r"(?:(\d+):)?(\d+):(\d+(?:\.\d+)?)", t)
    if m:
        return int(m.group(1) or 0) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
    return float(t) if _NUM.match(t) else None


def _split(line: str, delim: Optional[str]) -> List[str]:
    return [c.strip().strip('"') for c in (line.split(delim) if delim else re.split(r"[ \t]+", line.strip()))]


def _find_col(header: List[str], names: Tuple[str, ...]) -> int:
    for i, h in enumerate(header):
        base = re.sub(r"[（(].*$", "", h.strip().lower()).strip()
        if base in names:
            return i
    return -1


def parse_frames(text: str, time_unit: str = "auto") -> Tuple[List[Frame], Dict[str, Any]]:
    """Returns (frames, info). info: format, time_unit, lines, skipped, warnings."""
    text = text.lstrip("﻿")
    lines = text.splitlines()
    info: Dict[str, Any] = {"format": "text", "time_unit": time_unit, "lines": len(lines), "skipped": 0, "warnings": []}
    frames: List[Frame] = []
    delim = None
    cols = None                                         # column indexes when a header row was found
    unit_hint = ""
    for n, raw in enumerate(lines, 1):
        s = raw.strip()
        if not s or s.startswith(("//", "#", ";")) or s.lower().startswith(("date ", "base ", "begin ", "end ", "no internal")):
            continue
        if cols is None:
            for d in ("\t", ",", ";"):                  # a header row names an ID column and a data column
                hdr = _split(s, d)
                i_id, i_dt = _find_col(hdr, COL_ID), _find_col(hdr, COL_DATA)
                if len(hdr) > 2 and i_id >= 0 and i_dt >= 0 and not re.search(r"\d\.\d", s):
                    cols = {"id": i_id, "data": i_dt, "time": _find_col(hdr, COL_TIME), "dir": _find_col(hdr, COL_DIR),
                            "ch": _find_col(hdr, COL_CH)}
                    delim = d
                    info["format"] = "csv/table"
                    if cols["time"] >= 0:
                        th = hdr[cols["time"]].lower()
                        unit_hint = "ms" if "(ms)" in th or "毫秒" in th else "us" if ("(us)" in th or "μs" in th or "微秒" in th) else "s" if "(s)" in th or "秒" in th else ""
                    break
            if cols is not None:
                continue
        f = _parse_line(s, n, cols, delim)
        if f is None:
            info["skipped"] += 1
        else:
            frames.append(f)
    if frames and info["format"] == "text" and _ASC.match(next((l for l in lines if _ASC.match(l) or _ASC_FD.match(l)), "")):
        info["format"] = "vector asc"
    # time unit -> seconds
    unit = time_unit
    if unit == "auto":
        unit = unit_hint
    if not unit:
        raw_ts = [f.ts for f in frames]
        isint = all(float(t).is_integer() for t in raw_ts[:50]) if raw_ts else False
        unit = "us" if isint and raw_ts and (max(raw_ts) - min(raw_ts)) > 1e5 else "s"
        if isint and unit == "us":
            info["warnings"].append("时间标识是整数且跨度很大，按微秒处理；如果不对请指定 time_unit（s / ms / us）")
    k = {"s": 1.0, "ms": 1e-3, "us": 1e-6}.get(unit)
    if k is None:
        raise ValueError("time_unit 应为 auto / s / ms / us")
    for f in frames:
        f.ts = f.ts * k
    info["time_unit"] = unit
    info["frames"] = len(frames)
    if not frames:
        info["warnings"].append("没有识别出任何报文行：请确认格式（需要有 ID 和数据字节），或发几行样例给我")
    return frames, info


def _parse_line(s: str, n: int, cols: Optional[Dict[str, int]], delim: Optional[str]) -> Optional[Frame]:
    m = _ASC_FD.match(s)
    if m:
        data = _hex_bytes(m.group(6).split()) or b""
        cid = _parse_id(m.group(4))
        return Frame(float(m.group(1)), cid, data[:int(m.group(5))], int(m.group(2)), m.group(3).upper(), n) if cid is not None else None
    m = _ASC.match(s)
    if m:
        data = _hex_bytes(m.group(6).split()) or b""
        cid = _parse_id(m.group(3))
        return Frame(float(m.group(1)), cid, data[:int(m.group(5))], int(m.group(2)), m.group(4).upper(), n) if cid is not None else None
    if cols is not None:
        c = _split(s, delim)
        try:
            cid = _parse_id(c[cols["id"]])
            data = _hex_bytes(c[cols["data"]].split())
            if cid is None or data is None:
                return None
            ts = _parse_time(c[cols["time"]]) if cols["time"] >= 0 else None
            ch = int(c[cols["ch"]]) if cols["ch"] >= 0 and c[cols["ch"]].isdigit() else 0
            return Frame(ts if ts is not None else float(n), cid, data, ch, c[cols["dir"]] if cols["dir"] >= 0 else "", n)
        except (IndexError, ValueError):
            return None
    # plain lines: [time] [dir] ID [len] data...
    toks = [t for t in re.split(r"[\s,;\t]+", s) if t]
    ts = None
    if toks and ("." in toks[0] or ":" in toks[0]) and _parse_time(toks[0]) is not None:
        ts = _parse_time(toks.pop(0))
    while toks and toks[0].lower() in ("tx", "rx", "发送", "接收", "send", "receive", "d"):
        toks.pop(0)
    if len(toks) < 2:
        return None
    cid = _parse_id(toks[0])
    rest = toks[1:]
    if rest and re.fullmatch(r"\[?\d{1,2}\]?", rest[0]) and int(rest[0].strip("[]")) == len(rest) - 1:
        rest = rest[1:]
    data = _hex_bytes(rest)
    if cid is None or data is None:
        return None
    return Frame(ts if ts is not None else float(n), cid, data, 0, "", n)


# --------------------------------------------------------------------------- ISO-TP
def reassemble(frames: List[Frame]) -> List[Message]:
    msgs: List[Message] = []
    state: Dict[Tuple[int, int], dict] = {}
    for f in frames:
        d = f.data
        if not d:
            continue
        pci = d[0] >> 4
        key = (f.ch, f.can_id)
        if pci == 0:                                            # single frame (CAN FD: 00 LL escape)
            ln = d[0] & 0x0F
            off = 1
            if ln == 0 and len(d) > 8:
                ln, off = (d[1], 2) if len(d) > 1 else (0, 2)
            if 0 < ln <= len(d) - off:
                msgs.append(Message(f.ts, f.can_id, bytes(d[off:off + ln]), f.ts))
                state.pop(key, None)
        elif pci == 1 and len(d) >= 2:                          # first frame
            ln = ((d[0] & 0x0F) << 8) | d[1]
            off = 2
            if ln == 0 and len(d) >= 6:
                ln, off = int.from_bytes(d[2:6], "big"), 6
            if ln > 0:
                state[key] = {"len": ln, "buf": bytearray(d[off:]), "sn": 1, "t0": f.ts, "n": 1}
        elif pci == 2 and key in state:                         # consecutive frame
            st = state[key]
            if (d[0] & 0x0F) != st["sn"]:
                state.pop(key)
                continue
            st["sn"] = (st["sn"] + 1) & 0x0F
            st["buf"] += d[1:]
            st["n"] += 1
            if len(st["buf"]) >= st["len"]:
                msgs.append(Message(f.ts, f.can_id, bytes(st["buf"][:st["len"]]), st["t0"], st["n"]))
                state.pop(key)
    return msgs


def _is_response(resp: bytes, req: bytes) -> bool:
    if not resp or not req:
        return False
    return resp[0] == req[0] + 0x40 or (resp[0] == 0x7F and len(resp) >= 3 and resp[1] == req[0])


# --------------------------------------------------------------------------- UDS pairing
def pair(msgs: List[Message], tester_id: Optional[int] = None, ecu_id: Optional[int] = None) -> Tuple[List[Exchange], Dict[str, Any]]:
    """Pair requests with responses. Only (request ID -> response ID) pairs that really answer each other are used, so
    cyclic application frames on the same bus are ignored. tester_id / ecu_id restrict to one ECU."""
    last_req: Dict[int, Message] = {}
    pairs: Dict[Tuple[int, int], int] = {}
    for m in msgs:
        if not m.data:
            continue
        for rid, rq in list(last_req.items()):
            if rid != m.can_id and m.ts - rq.ts <= MAX_PAIR_DELAY_S and _is_response(m.data, rq.data):
                pairs[(rid, m.can_id)] = pairs.get((rid, m.can_id), 0) + 1
                del last_req[rid]
                break
        if m.data[0] in REQUEST_SIDS:
            last_req[m.can_id] = m
    if tester_id is not None:
        pairs = {k: v for k, v in pairs.items() if k[0] == tester_id}
    if ecu_id is not None:
        pairs = {k: v for k, v in pairs.items() if k[1] == ecu_id}
    info: Dict[str, Any] = {"pairs": [{"request_id": f"{a:X}", "response_id": f"{b:X}", "count": c}
                                      for (a, b), c in sorted(pairs.items(), key=lambda kv: -kv[1])], "warnings": []}
    if not pairs:
        info["warnings"].append("没有找到 UDS 请求/响应配对：请确认报文里有诊断报文，或用 tester_id / ecu_id 指定")
        return [], info
    req_ids = {a for a, _ in pairs}
    resp_of: Dict[int, set] = {}
    for a, b in pairs:
        resp_of.setdefault(a, set()).add(b)
    all_resp = {b for _, b in pairs}
    out: List[Exchange] = []
    pend: Optional[Message] = None
    pend_n = 0

    def flush(unanswered: Message) -> None:
        sup = len(unanswered.data) > 1 and bool(unanswered.data[1] & 0x80) and unanswered.data[0] in (0x10, 0x11, 0x3E, 0x28, 0x85, 0x83)
        out.append(Exchange(unanswered.ts, unanswered.ts, unanswered.can_id, 0, unanswered.data, None, pend_n, sup))

    for m in msgs:
        if not m.data:
            continue
        if pend is not None and m.can_id in (resp_of.get(pend.can_id, set()) if pend.can_id in resp_of else set()) | (all_resp if pend.can_id in FUNCTIONAL_IDS else set()) \
                and _is_response(m.data, pend.data):
            if m.data[0] == 0x7F and len(m.data) >= 3 and m.data[2] == 0x78:
                pend_n += 1
                continue
            out.append(Exchange(pend.ts, m.ts, pend.can_id, m.can_id, pend.data, m.data, pend_n))
            pend, pend_n = None, 0
            continue
        if (m.can_id in req_ids or m.can_id in FUNCTIONAL_IDS) and m.data[0] in REQUEST_SIDS:
            if pend is not None:
                flush(pend)
            pend, pend_n = m, 0
    if pend is not None:
        flush(pend)
    return out, info


def hexs(b: Optional[bytes]) -> str:
    return " ".join(f"{x:02X}" for x in b) if b else ""
