"""Creating modules and 功能 (executors) from outside the GUI, and turning a recorded trace into a draft 功能.
Stdlib only.  Everything is confined to the scan_data folder; nothing here touches a device."""
import os
from typing import Any, Dict, List, Optional

from . import flashgen, tracefile
from .engine import preflight
from .paths import home_dir, scan_dir
from .project import STEP_TYPES, fmt_hex, normalize_step, parse_id, read_global_security
from .scanlib import (FUNC_ADDR, INFO_FIELDS, Executor, Module, ScanError, ScanStore, check_name, default_info_executor,
                      function_paths)

MAX_TRACE_BYTES = 64 * 1024 * 1024
TRACE_EXT = (".csv", ".txt", ".asc", ".log", ".trc", ".tsv")
RESULT_KEYS = ("step", "skip", "parse")
MAX_EXCHANGES_SHOWN = 300


class FuncError(Exception):
    pass


def _store(home: Optional[str]) -> ScanStore:
    s = ScanStore(scan_dir(home))
    s.ensure_defaults()
    return s


def _module(store: ScanStore, name: str) -> Module:
    for m in store.modules():
        if m.name == name:
            return m
    raise FuncError(f"找不到软件模块「{name}」，可用: {', '.join(x.name for x in store.modules())}")


def _func_path(store: ScanStore, m: Module, path: str) -> str:
    parts = [p for p in str(path).replace("\\", "/").split("/") if p != ""]
    if not parts:
        raise FuncError("功能路径不能为空，如 '烧写' 或 '标定/零位标定'")
    try:
        parts = [check_name(p) for p in parts]
    except ScanError as e:
        raise FuncError(str(e))
    p = os.path.join(store.func_dir(m), *parts[:-1], parts[-1] + ".json")
    try:
        store._inside(p)
    except ScanError as e:
        raise FuncError(str(e))
    return p


def check_steps(steps: Any, result: Optional[dict], home: Optional[str] = None) -> Dict[str, Any]:
    """Validate a list of steps (the same vocabulary as the ECU刷新 plan). Returns {"ok", "problems", "steps"}."""
    problems: List[str] = []
    if not isinstance(steps, list) or not steps:
        return {"ok": False, "problems": ["steps 应为非空列表"], "steps": []}
    norm: List[dict] = []
    for i, s in enumerate(steps, 1):
        if not isinstance(s, dict) or s.get("type") not in STEP_TYPES:
            problems.append(f"步骤{i}: type 应为 {'/'.join(STEP_TYPES)}")
            continue
        try:
            norm.append(normalize_step(s))
        except (ValueError, TypeError) as e:
            problems.append(f"步骤{i}: {e}")
    if len(norm) == len(steps):
        sec = read_global_security(home_dir(home))
        problems += preflight(norm, home_dir(home), sec.key_path(home_dir(home)) if sec else "", sec.level if sec else None)
    res = {"step": -1, "skip": -1, "parse": "hex"}
    if result is not None:
        if not isinstance(result, dict) or set(result) - set(RESULT_KEYS):
            problems.append(f"result 只能包含 {', '.join(RESULT_KEYS)}")
        else:
            res.update(result)
            if res["parse"] not in ("hex", "ascii", "dec"):
                problems.append("result.parse 应为 hex / ascii / dec")
            for k in ("step", "skip"):
                if not isinstance(res[k], int) or isinstance(res[k], bool) or res[k] < -1 or (k == "step" and res[k] >= len(norm)):
                    problems.append(f"result.{k} 应为 -1（自动）或有效的整数")
    return {"ok": not problems, "problems": problems, "steps": norm, "result": res}


def create_module(name: str, phys: str, resp: str = "", func: str = "", home: Optional[str] = None) -> Dict[str, Any]:
    store = _store(home)
    try:
        nm = check_name(name)
        p = parse_id(phys)
        r = parse_id(resp) if resp else -1
        f = parse_id(func) if func else FUNC_ADDR
    except (ScanError, ValueError) as e:
        raise FuncError(f"模块参数无效: {e}")
    if any(m.name == nm or m.folder == nm for m in store.modules()):
        raise FuncError(f"模块「{nm}」已存在")
    m = Module(nm, p, r, f, order=len(store.modules()), folder=nm)
    store.save_module(m)
    for key, _ in INFO_FIELDS:                          # the standard scan executors, so 软件扫描 works right away
        default_info_executor(key).save(store.info_path(m, key))
    os.makedirs(store.func_dir(m), exist_ok=True)
    return {"created": True, "module": m.to_dict(), "folder": store.module_dir(m)}


def read_function(module: str, path: str, home: Optional[str] = None) -> Dict[str, Any]:
    store = _store(home)
    m = _module(store, module)
    p = _func_path(store, m, path)
    if not os.path.isfile(p):
        raise FuncError(f"模块「{module}」里没有功能「{path}」，可用: {', '.join(function_paths(store, module)) or '(无)'}")
    try:
        ex = Executor.load(p)
    except ScanError as e:
        raise FuncError(str(e))
    return {"module": module, "path": path, "name": ex.name, "steps": ex.steps, "result": ex.result,
            "problems": check_steps(ex.steps, ex.result, home)["problems"]}


def save_function(module: str, path: str, steps: Any, result: Optional[dict] = None, overwrite: bool = False,
                  allow_incomplete: bool = False, home: Optional[str] = None) -> Dict[str, Any]:
    """Create / replace a 功能 of a module. Incomplete steps (no file / key DLL yet) are refused unless allow_incomplete."""
    store = _store(home)
    m = _module(store, module)
    p = _func_path(store, m, path)
    chk = check_steps(steps, result, home)
    structural = [x for x in chk["problems"] if "type 应为" in x or "steps 应为" in x or x.startswith("result")]
    if structural or (chk["problems"] and not allow_incomplete):
        return {"saved": False, "reason": "功能校验未通过，未保存（还缺文件/密钥等的草稿可设置 allow_incomplete=true）",
                "problems": chk["problems"]}
    if os.path.exists(p) and not overwrite:
        return {"saved": False, "reason": f"已存在同名功能「{path}」，如要覆盖请设置 overwrite=true", "problems": chk["problems"]}
    ex = Executor(name=str(path).replace("\\", "/").split("/")[-1], result=chk["result"])
    ex.project.steps = chk["steps"]
    ex.save(p)
    return {"saved": True, "path": p, "problems": chk["problems"],
            "message": "已保存。" + ("还有未补全的内容，执行前请补全。" if chk["problems"] else "")}


def delete_function(module: str, path: str, home: Optional[str] = None) -> Dict[str, Any]:
    store = _store(home)
    m = _module(store, module)
    p = _func_path(store, m, path)
    if not os.path.isfile(p):
        raise FuncError(f"没有功能「{path}」")
    os.remove(p)
    return {"deleted": True, "path": path}


# --------------------------------------------------------------------------- trace analysis
def _compact(exchanges: List[tracefile.Exchange]) -> List[Dict[str, Any]]:
    """Exchanges for the agent to read: runs of 36 TransferData collapse into one line."""
    out: List[Dict[str, Any]] = []
    t0 = exchanges[0].t_req if exchanges else 0.0
    i = 0
    while i < len(exchanges):
        e = exchanges[i]
        if e.sid == 0x36:
            j = i
            total = 0
            while j < len(exchanges) and exchanges[j].sid == 0x36:
                total += max(len(exchanges[j].req) - 2, 0)
                j += 1
            out.append({"n": i + 1, "t": round(e.t_req - t0, 3), "service": "36 TransferData", "blocks": j - i, "bytes": total,
                        "all_positive": all(x.ok for x in exchanges[i:j])})
            i = j
            continue
        out.append({"n": i + 1, "t": round(e.t_req - t0, 3), "req_id": f"{e.req_id:X}", "req": tracefile.hexs(e.req)[:120],
                    "resp": tracefile.hexs(e.resp)[:120] if e.resp else None,
                    **({"pending_78": e.pending} if e.pending else {}), **({"suppressed": True} if e.suppressed else {})})
        i += 1
    return out


def analyze_trace(text: str = "", path: str = "", tester_id: str = "", ecu_id: str = "", time_unit: str = "auto",
                  delay_threshold_s: float = 0.5) -> Dict[str, Any]:
    """Read a recorded trace (ZCANPro / CanVDS CSV export, Vector ASC, "ID data" lines), pair the UDS exchanges and draft the
    steps of a 功能 from them (see zdiag.flashgen)."""
    if bool(text) == bool(path):
        raise FuncError("text（报文文本）和 path（本机报文文件路径）二选一")
    if path:
        if not path.lower().endswith(TRACE_EXT):
            raise FuncError(f"报文文件扩展名应为 {' / '.join(TRACE_EXT)}")
        try:
            if os.path.getsize(path) > MAX_TRACE_BYTES:
                raise FuncError("报文文件太大（超过 64MB）")
            with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
                text = f.read()
        except OSError as e:
            raise FuncError(f"读取报文文件失败: {e}")
    try:
        frames, finfo = tracefile.parse_frames(text, time_unit)
        ti = parse_id(tester_id) if tester_id else None
        ei = parse_id(ecu_id) if ecu_id else None
    except ValueError as e:
        raise FuncError(str(e))
    exchanges, pinfo = tracefile.pair(tracefile.reassemble(frames), ti, ei)
    res: Dict[str, Any] = {"format": finfo["format"], "time_unit": finfo["time_unit"], "frames": finfo.get("frames", 0),
                           "skipped_lines": finfo["skipped"], "pairs": pinfo["pairs"], "exchange_count": len(exchanges),
                           "warnings": finfo["warnings"] + pinfo["warnings"]}
    if not exchanges:
        return {**res, "exchanges": [], "draft": None}
    first = pinfo["pairs"][0]
    res["suggested_module"] = {"phys": first["request_id"], "resp": first["response_id"]}
    shown = _compact(exchanges)
    res["exchanges"] = shown[:MAX_EXCHANGES_SHOWN]
    if len(shown) > MAX_EXCHANGES_SHOWN:
        res["warnings"].append(f"交换太多，只显示前 {MAX_EXCHANGES_SHOWN} 条（草稿使用全部）")
    res["draft"] = flashgen.draft(exchanges, delay_threshold_s)
    return res


FUNCTION_DOC = """\
CanVDS「功能」(执行器) = 某个软件模块下的一串步骤，和「ECU刷新」的方案是同一套步骤；保存在 scan_data/<模块>/功能/<路径>.json。
在软件扫描窗口、流程编排的「执行功能」节点、run_function / start_function 里都能执行。

步骤 = {"type": ..., "name": 显示名, ...字段}，未写的字段用默认值。类型:
- service: pdu(十六进制 "10 03"), addr("phys"|"func"), suppress(true=不期望响应), check(true=校验响应), expect(期望响应模式，空=肯定响应)
- delay: ms
- unlock: 不用写 level 和 dll——安全级别（security.level，奇数，默认 1）和密钥 DLL（security.key_dll）都是全局设置（settings_set，配置一次永久有效），每次执行时现取最新值，已有功能不用逐个改；"builtin:xor"=演示算法
- driver: file(Flash driver 文件 hex/s19/bin 路径), base_addr(十六进制，bin才用), addr_len(4), size_len(4), dfi("00")
- download: 同 driver，下载应用程序；多段 hex/s19 的各段地址以文件为准
- routine(31例程):
    rkind="erase": 擦除内存 rid("FF 00"), sub("01"), addr_len, size_len, ext_on/ext(擦除内存扩展字节)；范围由烧写文件决定
    rkind="crc": CRC校验 rid(分段CRC的例程标识符), rid_file(全文件CRC的标识符，可空), crc_mode("region"分段+全文件 | "file"只做全文件),
                 crc{name,width,poly,init,xorout,refin,refout}(必须与ECU算法一致), crc_order("big"|"little")
    rkind="custom": 通用31请求 sub, rid("DF FE"), data(十六进制，可用占位符 {addr} {len} {crc}), expect, status_ok
文件路径写绝对路径，或相对 CanVDS 的目录。
返回值: result = {"step": 取哪一步的响应(-1=最后一个有响应的步骤), "skip": 跳过响应前几个字节(-1=自动), "parse": "hex"|"ascii"|"dec"}。

典型烧写顺序: 会话控制(10 03/02) → [关闭DTC/通信 85/28] → 解锁(27) → driver下载 → 擦除(31 FF00) → 应用下载 → CRC校验 → 依赖性检查(31 FF01) → 复位(11 01) → 等待 → 读版本确认。

从报文生成: analyze_trace(text 或 path) → 得到交换列表和 draft(steps/todo/warnings/notes) → 补全 todo 里列出的内容(文件、密钥DLL、CRC算法；这些报文里不可能有)
→ check_function 校验 → save_function(module, path, steps) → 需要时可先在模拟ECU上用 start_function 试跑(只能验证步骤是否通顺) → 再真机执行。
注意: 烧写会改写 ECU，路径/文件/密钥/CRC 错误可能使 ECU 无法启动；不确定的参数(尤其文件、CRC算法、擦除范围)必须向用户确认，不要猜。
"""
