"""Turn the UDS exchanges of a recorded session (zdiag.tracefile) into a draft 功能 (a list of steps in the same vocabulary as
the ECU刷新 plan: service / delay / unlock / driver / download / routine).  The draft is a starting point: files, key DLL and
CRC parameters cannot be known from a trace, so they are listed under `todo` for the user / agent to fill in."""
from typing import Any, Dict, List, Optional, Tuple

from .project import fmt_hex, new_step
from .tracefile import SERVICE_NAME, Exchange, hexs

SID_ERASE_HINT = {0xFF00}                       # ISO 14229 eraseMemory routine id (only a hint; the body layout decides)
SUBFUNC_NAME = {0x10: {1: "默认会话", 2: "编程会话", 3: "扩展会话"}, 0x11: {1: "硬复位", 2: "钥匙复位", 3: "软复位"}}


def _parse_alfi(alfi: int) -> Tuple[int, int]:
    return alfi & 0x0F, alfi >> 4                       # (address length, size length)


def _routine_body_is_erase(body: bytes) -> Optional[Dict[str, int]]:
    """31 01 <rid> <fmt> <addr> <size> [ext] -> parsed values, or None when the body does not look like that."""
    if len(body) < 3:
        return None
    al, sl = _parse_alfi(body[0])
    if not (1 <= al <= 4 and 1 <= sl <= 4):
        return None
    need = 1 + al + sl
    if len(body) not in (need, need + 1):
        return None
    return {"al": al, "sl": sl, "addr": int.from_bytes(body[1:1 + al], "big"), "size": int.from_bytes(body[1 + al:need], "big"),
            "ext": body[need] if len(body) == need + 1 else -1}


def draft(exchanges: List[Exchange], delay_threshold_s: float = 0.5, func_id: int = 0x7DF) -> Dict[str, Any]:
    """Returns {"steps": [...], "todo": [...], "warnings": [...], "notes": [...]}."""
    steps: List[Dict[str, Any]] = []
    todo: List[str] = []
    warnings: List[str] = []
    notes: List[str] = []
    cycles: List[Dict[str, Any]] = []                   # every 34..37 cycle: addr, size, alfi, dfi, blocks, bytes
    erase_seen = False
    i, n = 0, len(exchanges)
    last_end: Optional[float] = None

    def bad(ex: Exchange, what: str) -> None:
        if ex.resp is None and not ex.suppressed:
            warnings.append(f"{what}（{hexs(ex.req)}）在报文里没有得到响应")
        elif ex.nrc is not None and ex.nrc != 0x78:
            warnings.append(f"{what}（{hexs(ex.req)}）在报文里得到的是否定响应 NRC 0x{ex.nrc:02X}，请确认这一步该不该保留")

    def delay_before(ex: Exchange) -> None:
        if last_end is not None and ex.t_req - last_end >= delay_threshold_s:
            ms = int(round((ex.t_req - last_end) * 10)) * 100
            steps.append(new_step("delay", name=f"等待 {ms / 1000:g} 秒", ms=ms))

    while i < n:
        ex = exchanges[i]
        sid, req = ex.sid, ex.req
        delay_before(ex)
        if sid == 0x3E:                                           # tester present: the keep-alive setting covers it
            if "TesterPresent" not in " ".join(notes):
                notes.append("忽略了 TesterPresent(3E)：请在设置里启用「保持会话」代替")
            i += 1
            continue
        if sid == 0x27 and len(req) >= 2 and req[1] % 2 == 1:     # request seed ... send key -> one unlock step
            level = req[1]
            j = i + 1
            if j < n and exchanges[j].sid == 0x27 and len(exchanges[j].req) >= 2 and exchanges[j].req[1] == level + 1:
                bad(exchanges[j], "发送密钥")
                last_end = exchanges[j].t_resp if exchanges[j].resp else exchanges[j].t_req
                i = j + 1
            else:
                warnings.append(f"安全访问 27 {level:02X} 后面没有对应的发送密钥(27 {level + 1:02X})")
                last_end = ex.t_resp if ex.resp else ex.t_req
                i += 1
            bad(ex, "请求种子")
            steps.append(new_step("unlock", name=f"ECU解锁 级别{level:02X}", level=level))
            todo.append(f"解锁步骤(级别 {level:02X})：需要选择密钥 DLL（dll 字段）")
            continue
        if sid == 0x34 and len(req) >= 3:                        # download cycle 34 .. 36 .. 37
            al, sl = _parse_alfi(req[2])
            need = 3 + al + sl
            cyc = {"dfi": req[1], "al": al, "sl": sl, "addr": int.from_bytes(req[3:3 + al], "big") if len(req) >= need else 0,
                   "size": int.from_bytes(req[3 + al:need], "big") if len(req) >= need else 0, "blocks": 0, "bytes": 0,
                   "before_erase": not erase_seen, "step": len(steps), "exited": False}
            bad(ex, "请求下载")
            j = i + 1
            while j < n and exchanges[j].sid == 0x36:
                cyc["blocks"] += 1
                cyc["bytes"] += max(len(exchanges[j].req) - 2, 0)
                bad(exchanges[j], "传输数据")
                j += 1
            if j < n and exchanges[j].sid == 0x37:
                cyc["exited"] = True
                bad(exchanges[j], "退出传输")
                j += 1
            else:
                warnings.append(f"下载 0x{cyc['addr']:X} 之后没有看到退出传输(37)")
            if cyc["bytes"] != cyc["size"]:
                warnings.append(f"下载 0x{cyc['addr']:X} 声明长度 {cyc['size']} 字节，实际传输 {cyc['bytes']} 字节")
            cycles.append(cyc)
            last_end = exchanges[j - 1].t_resp if exchanges[j - 1].resp else exchanges[j - 1].t_req
            steps.append({"_cycle": cyc})
            i = j
            continue
        if sid == 0x31 and len(req) >= 4:
            sub, rid, body = req[1], req[2:4], req[4:]
            rid_i = int.from_bytes(rid, "big")
            er = _routine_body_is_erase(body) if sub == 1 else None
            if er is not None and (rid_i in SID_ERASE_HINT or not cycles):
                bad(ex, "擦除内存")
                erase_seen = True
                for c in cycles:
                    c["before_erase"] = True
                steps.append(new_step("routine", name="擦除内存", rkind="erase", sub=f"{sub:02X}", rid=fmt_hex(rid), addr_len=er["al"],
                                      size_len=er["sl"], ext_on=er["ext"] >= 0, ext=f"{er['ext']:02X}" if er["ext"] >= 0 else "01"))
                notes.append(f"擦除范围在报文里是 0x{er['addr']:X} 大小 0x{er['size']:X}；步骤里的擦除范围由烧写文件决定，请确认文件与之一致")
            elif sub == 1 and cycles and cycles[-1]["exited"] and 1 <= len(body) <= 4:
                bad(ex, "CRC 校验")
                steps.append({"_crc": {"rid": bytes(rid), "len": len(body)}})
            else:
                bad(ex, "例程")
                steps.append(new_step("routine", name=f"例程 {fmt_hex(rid)}", rkind="custom", sub=f"{sub:02X}", rid=fmt_hex(rid),
                                      data=fmt_hex(body), expect=""))
            last_end = ex.t_resp if ex.resp else ex.t_req
            i += 1
            continue
        # everything else: one service step
        name = SERVICE_NAME.get(sid, f"服务 {sid:02X}")
        if len(req) > 1 and sid in SUBFUNC_NAME:
            name += " " + SUBFUNC_NAME[sid].get(req[1] & 0x7F, f"{req[1] & 0x7F:02X}")
        bad(ex, name)
        steps.append(new_step("service", name=name, addr="func" if ex.req_id == func_id else "phys", pdu=hexs(req), suppress=ex.suppressed))
        last_end = ex.t_resp if ex.resp else ex.t_req
        i += 1

    return _finish(steps, cycles, todo, warnings, notes)


def _finish(steps: List[dict], cycles: List[dict], todo: List[str], warnings: List[str], notes: List[str]) -> Dict[str, Any]:
    """Cycle / CRC markers -> driver / download / crc steps. Consecutive cycles of the same kind (only CRC routines in between)
    become ONE download step followed by ONE crc step."""
    multi = any(c["before_erase"] for c in cycles) and any(not c["before_erase"] for c in cycles)
    biggest = max([c["size"] for c in cycles] or [0])
    out: List[Dict[str, Any]] = []
    cur: Optional[Dict[str, Any]] = None                # {"step","kind","cycles","crcs","pending"}

    def kind_of(c: dict) -> str:
        if multi:
            return "driver" if c["before_erase"] else "download"
        if len(cycles) > 1 and c is cycles[0] and c["size"] < biggest / 4:
            return "driver"                             # no erase to tell them apart: a much smaller first image is the driver
        return "download"

    def flush() -> None:
        nonlocal cur
        if cur is None:
            return
        marks = cur["crcs"] + cur["pending"]
        if marks:
            rids: List[bytes] = []
            for m in marks:
                if m["rid"] not in rids:
                    rids.append(m["rid"])
            region = len(marks) > 1 or len(rids) > 1
            out.append(new_step("routine", name="CRC校验", rkind="crc", sub="01", rid=fmt_hex(rids[0]),
                                rid_file=fmt_hex(rids[1]) if len(rids) > 1 else "", crc_mode="region" if region else "file"))
            todo.append(f"CRC 校验：报文里 CRC 为 {marks[0]['len']} 字节，请设置与 ECU 一致的算法（crc：宽度/多项式/初值/异或/反转）"
                        f"和字节序（crc_order）；CRC 例程标识符 {', '.join(fmt_hex(r) for r in rids)}")
        cur = None

    for s in steps:
        if "_cycle" in s:
            c = s["_cycle"]
            kind = kind_of(c)
            if cur is not None and cur["kind"] == kind:
                cur["cycles"].append(c)
                cur["crcs"] += cur["pending"]
                cur["pending"] = []
                continue
            flush()
            label = "Flash driver" if kind == "driver" else "文件下载"
            st = new_step(kind, name=label, base_addr=f"{c['addr']:X}", addr_len=c["al"], size_len=c["sl"], dfi=f"{c['dfi']:02X}")
            out.append(st)
            cur = {"step": st, "kind": kind, "cycles": [c], "crcs": [], "pending": []}
        elif "_crc" in s:
            if cur is not None:
                cur["pending"].append(s["_crc"])
            else:
                out.append(new_step("routine", name="例程 " + fmt_hex(s["_crc"]["rid"]), rkind="custom", sub="01",
                                    rid=fmt_hex(s["_crc"]["rid"]), data=""))
        else:
            flush()
            out.append(s)
    flush()
    # file placeholders + todo
    groups = {}
    for c in cycles:
        groups.setdefault(kind_of(c), []).append(c)
    for st in out:
        if st["type"] in ("driver", "download"):
            cs = groups.get(st["type"], [])
            st["file"] = ""
            segs = ", ".join(f"0x{c['addr']:X}/{c['bytes']}字节" for c in cs)
            todo.append(f"「{st['name']}」：需要选择文件（file 字段，hex/s19/bin 路径）；报文里的段是 {segs}")
            if st["type"] == "download" and cs and cs[0]["addr"]:
                notes.append(f"「{st['name']}」base_addr 取自报文首段 0x{cs[0]['addr']:X}（hex/s19 文件的地址以文件为准，bin 才用它）")
    return {"steps": out, "todo": todo, "warnings": warnings, "notes": notes}
