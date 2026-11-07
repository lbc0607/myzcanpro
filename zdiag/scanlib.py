"""软件扫描: modules (software on the multi-core board), executors stored as JSON in a folder tree (no Qt).

Folder layout (the hierarchy of the screens == the hierarchy of the folders):
    <root>/<module>/module.json          name, physical/response/functional address, order
    <root>/<module>/信息/<field>.json    executors behind 软件编码 / 软件版本 / 软件日期 / 故障码数量
    <root>/<module>/功能/...             user functions: *.json = executor button, sub-folder = a sub-menu
"""
from __future__ import annotations
import copy
import json
import os
import shutil
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .engine import DiagSession, preflight
from .project import Addressing, DiagConfig, Project, fmt_hex, new_step, normalize_step, parse_id

FORMAT = "myzcanpro-executor"
INFO_DIR, FUNC_DIR, MODULE_FILE = "信息", "功能", "module.json"
INFO_FIELDS = [("code", "软件编码"), ("version", "软件版本"), ("date", "硬件版本"), ("dtc", "故障码数量")]
FIELD_LABEL = dict(INFO_FIELDS)
FUNC_ADDR = 0x7DF
BAD_NAME = '\\/:*?"<>|'


class ScanError(Exception):
    pass


# --------------------------------------------------------------------------- modules
@dataclass
class Module:
    name: str
    phys: int
    resp: int = -1                 # -1: physical + 8
    func: int = FUNC_ADDR
    order: int = 0
    folder: str = ""               # directory name below the root (fixed when the module was created)

    @property
    def resp_id(self) -> int:
        return self.phys + 8 if self.resp < 0 else self.resp

    def addr(self) -> Addressing:
        return Addressing(phys=self.phys, func=self.func, resp=self.resp_id)

    def to_dict(self) -> dict:
        return {"name": self.name, "phys": f"{self.phys:X}", "resp": f"{self.resp_id:X}",
                "func": f"{self.func:X}", "order": self.order}

    @staticmethod
    def from_dict(d: dict, folder: str) -> "Module":
        phys = parse_id(str(d["phys"]))
        resp = parse_id(str(d["resp"])) if d.get("resp") not in (None, "") else phys + 8
        return Module(str(d.get("name") or folder), phys, resp, parse_id(str(d.get("func", "7DF"))),
                      int(d.get("order", 0)), folder)


DEFAULT_MODULES = [("动力域控制器", 0x7E2), ("驱动电机控制器", 0x7E3), ("电驱充电模块", 0x7E3), ("从驱动电机控制器", 0x745)]


def cfg_for(base: DiagConfig, m: Module) -> DiagConfig:
    """Global diagnostic settings with the module's addresses."""
    c = copy.deepcopy(base)
    a = m.addr()
    c.addr.phys, c.addr.func, c.addr.resp = a.phys, a.func, a.resp
    return c


# --------------------------------------------------------------------------- executors
@dataclass
class Executor:
    name: str = "新建功能"
    project: Project = None
    result: Dict = None

    def __post_init__(self) -> None:
        if self.project is None:
            self.project = Project(name=self.name)
        if self.result is None:
            self.result = {"step": -1, "skip": -1, "parse": "hex"}

    @property
    def steps(self) -> List[dict]:
        return self.project.steps

    def to_dict(self) -> dict:
        return {"format": FORMAT, "version": 1, "name": self.name, "steps": self.project.steps, "result": self.result}

    @staticmethod
    def from_dict(d: dict) -> "Executor":
        if d.get("format") != FORMAT:
            raise ScanError("不是 CanVDS 执行器文件")
        steps = [normalize_step(s) for s in d.get("steps", [])]
        res = {"step": -1, "skip": -1, "parse": "hex"}
        res.update(d.get("result") or {})
        return Executor(d.get("name", "未命名"), Project(name=d.get("name", "未命名"), steps=steps), res)

    @staticmethod
    def load(path: str) -> "Executor":
        try:
            with open(path, "r", encoding="utf-8") as f:
                ex = Executor.from_dict(json.load(f))
        except (OSError, ValueError, KeyError) as e:
            raise ScanError(f"读取执行器失败 {path}: {e}")
        return ex

    def save(self, path: str) -> None:
        os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
        with open(path, "w", encoding="utf-8") as f:
            json.dump(self.to_dict(), f, ensure_ascii=False, indent=2)


def read_did_executor(name: str, did: str, skip: int = -1, pre: str = "10 03") -> Executor:
    steps = []
    if pre:
        steps.append(new_step("service", name="进入扩展会话", pdu=pre))
    steps.append(new_step("service", name=name, pdu=f"22 {did}"))
    return Executor(name, Project(name=name, steps=steps), {"step": -1, "skip": skip, "parse": "hex"})


def default_info_executor(key: str) -> Executor:
    # ISO 14229-1 Annex C: F194 supplier ECU software number, F195 software version, F193 hardware version; editable per software in the GUI.
    if key == "code":
        return read_did_executor("读取软件编码", "F1 94")
    if key == "version":
        return read_did_executor("读取软件版本", "F1 95")
    if key == "date":                                     # key stays "date" so existing data folders keep working
        return read_did_executor("读取硬件版本", "F1 93")
    if key == "dtc":                                      # 19 01 <mask>: 59 01 <availability> <format> <count hi> <count lo>
        ex = Executor("读取故障码数量", Project(name="读取故障码数量", steps=[
            new_step("service", name="进入扩展会话", pdu="10 03"),
            new_step("service", name="读取故障码数量", pdu="19 01 08")]), {"step": -1, "skip": 4, "parse": "hex"})
        return ex
    raise KeyError(key)


def default_function_executors() -> Dict[str, Executor]:
    dtc = Executor("故障检测", Project(name="故障检测", steps=[
        new_step("service", name="进入扩展会话", pdu="10 03"),
        new_step("service", name="读取故障码", pdu="19 02 08")]), {"step": -1, "skip": 3, "parse": "hex"})
    clr = Executor("清除故障", Project(name="清除故障", steps=[
        new_step("service", name="进入扩展会话", pdu="10 03"),
        new_step("service", name="清除DTC", pdu="14 FF FF FF")]), {"step": -1, "skip": -1, "parse": "hex"})
    return {"故障检测": dtc, "清除故障": clr}


def default_skip(pdu: bytes) -> int:
    """Bytes of the positive reply that are only echo of the request: 62 + DID for 22, else just the SID."""
    return 3 if pdu and pdu[0] == 0x22 else 1


PARSE_MODES = [("hex", "十六进制原文"), ("ascii", "ASCII 文本"), ("dec", "十进制数值(大端)")]


def render_result(raw: bytes, parse: str) -> str:
    """Bytes -> the text shown for a result (hex original / ASCII text such as a VIN / big-endian decimal)."""
    if parse == "ascii":
        return "".join(chr(b) if 32 <= b < 127 else "." for b in raw).strip()
    if parse == "dec":
        return str(int.from_bytes(raw, "big")) if raw else ""
    return fmt_hex(raw)


def extract_result(ex: Executor, replies: Dict[int, bytes]) -> Optional[str]:
    """The text shown for an executor: the chosen step's reply without the echoed header, rendered per
    result.parse (hex by default).  None when that step produced no positive reply."""
    raw = extract_raw(ex, replies)
    return None if raw is None else render_result(raw, str(ex.result.get("parse", "hex")))


def extract_raw(ex: Executor, replies: Dict[int, bytes]) -> Optional[bytes]:
    """The result bytes (reply of the chosen step minus the echoed header); None without a positive reply."""
    steps = ex.steps
    idx = int(ex.result.get("step", -1))
    if idx < 0:
        idx = max(replies) if replies else -1
    data = replies.get(idx)
    if data is None:
        return None
    skip = int(ex.result.get("skip", -1))
    if skip < 0:
        from .project import parse_hex
        try:
            skip = default_skip(parse_hex(steps[idx]["pdu"]))
        except (IndexError, KeyError, ValueError):
            skip = 1
    return bytes(data[skip:])


@dataclass
class Outcome:
    ok: bool
    text: str = ""                 # rendered result ("" = nothing after the header)
    error: str = ""
    raw: bytes = b""               # the result bytes (for numeric comparisons)


def run_executor(session: DiagSession, ex: Executor) -> Outcome:
    steps = copy.deepcopy(ex.steps)
    if not steps:
        return Outcome(False, error="执行器里没有步骤")
    problems = preflight(steps, session.base_dir)
    if problems:
        return Outcome(False, error="; ".join(problems))
    ok = session.run_plan(steps)
    if not ok:
        return Outcome(False, error=session.last_error or "已取消")
    raw = extract_raw(ex, session.replies) or b""
    return Outcome(True, render_result(raw, str(ex.result.get("parse", "hex"))), raw=raw)


# --------------------------------------------------------------------------- store
def check_name(name: str) -> str:
    name = name.strip()
    if not name or name in (".", "..") or any(c in BAD_NAME for c in name):
        raise ScanError('名称不能为空，且不能包含 \\ / : * ? " < > |')
    return name


@dataclass
class Entry:
    name: str
    path: str
    is_dir: bool


class ScanStore:
    def __init__(self, root: str) -> None:
        self.root = root

    # -- seeding / modules
    def ensure_defaults(self) -> None:
        os.makedirs(self.root, exist_ok=True)
        if self.modules():
            return
        for i, (name, phys) in enumerate(DEFAULT_MODULES):
            m = Module(name, phys, order=i, folder=name)
            self.save_module(m)
            for key, _ in INFO_FIELDS:
                default_info_executor(key).save(self.info_path(m, key))
            for fname, ex in default_function_executors().items():
                ex.save(os.path.join(self.func_dir(m), fname + ".json"))

    def modules(self) -> List[Module]:
        out = []
        if not os.path.isdir(self.root):
            return out
        for d in sorted(os.listdir(self.root)):
            f = os.path.join(self.root, d, MODULE_FILE)
            if os.path.isfile(f):
                try:
                    with open(f, "r", encoding="utf-8") as fh:
                        out.append(Module.from_dict(json.load(fh), d))
                except (OSError, ValueError, KeyError):
                    continue
        return sorted(out, key=lambda m: (m.order, m.folder))

    def save_module(self, m: Module) -> None:
        d = os.path.join(self.root, m.folder)
        os.makedirs(d, exist_ok=True)
        with open(os.path.join(d, MODULE_FILE), "w", encoding="utf-8") as f:
            json.dump(m.to_dict(), f, ensure_ascii=False, indent=2)

    def module_dir(self, m: Module) -> str:
        return os.path.join(self.root, m.folder)

    def func_dir(self, m: Module) -> str:
        return os.path.join(self.module_dir(m), FUNC_DIR)

    # -- info executors
    def info_path(self, m: Module, key: str) -> str:
        return os.path.join(self.module_dir(m), INFO_DIR, FIELD_LABEL[key] + ".json")

    def load_info(self, m: Module, key: str) -> Executor:
        p = self.info_path(m, key)
        if not os.path.isfile(p):
            default_info_executor(key).save(p)
        return Executor.load(p)

    def save_info(self, m: Module, key: str, ex: Executor) -> None:
        ex.save(self.info_path(m, key))

    # -- folder tree (functions)
    def list_entries(self, directory: str) -> List[Entry]:
        os.makedirs(directory, exist_ok=True)
        dirs, files = [], []
        for n in os.listdir(directory):
            p = os.path.join(directory, n)
            if os.path.isdir(p):
                dirs.append(Entry(n, p, True))
            elif n.lower().endswith(".json") and n != MODULE_FILE:
                files.append(Entry(n[:-5], p, False))
        return sorted(dirs, key=lambda e: e.name) + sorted(files, key=lambda e: e.name)

    def _inside(self, path: str) -> str:
        ap, ar = os.path.abspath(path), os.path.abspath(self.root)
        if os.path.commonpath([ap, ar]) != ar:
            raise ScanError("路径超出了数据文件夹")
        return ap

    def make_folder(self, parent: str, name: str) -> str:
        p = os.path.join(self._inside(parent), check_name(name))
        if os.path.exists(p) or os.path.exists(p + ".json"):
            raise ScanError(f"「{name}」已存在")
        os.makedirs(p)
        return p

    def executor_path(self, parent: str, name: str) -> str:
        return os.path.join(self._inside(parent), check_name(name) + ".json")

    def save_new(self, parent: str, name: str, ex: Executor) -> str:
        p = self.executor_path(parent, name)
        if os.path.exists(p) or os.path.isdir(p[:-5]):
            raise ScanError(f"「{name}」已存在")
        ex.name = name
        ex.save(p)
        return p

    def rename(self, e: Entry, new: str) -> str:
        new = check_name(new)
        src = self._inside(e.path)
        dst = os.path.join(os.path.dirname(src), new + ("" if e.is_dir else ".json"))
        if os.path.exists(dst) or os.path.exists(dst[:-5] if dst.endswith(".json") else dst + ".json"):
            raise ScanError(f"「{new}」已存在")
        os.rename(src, dst)
        if not e.is_dir:
            try:
                ex = Executor.load(dst)
                ex.name = new
                ex.save(dst)
            except ScanError:
                pass
        return dst

    def delete(self, e: Entry) -> None:
        p = self._inside(e.path)
        if e.is_dir:
            shutil.rmtree(p)
        else:
            os.remove(p)


# --------------------------------------------------------------------------- scanning
def scan_module(store: ScanStore, m: Module, open_session: Callable[[Module], DiagSession], cancel,
                on_field: Optional[Callable[[str, str], None]] = None) -> Dict[str, str]:
    """Read all four info fields of a module. Unread / failed fields are "-"."""
    res = {k: "-" for k, _ in INFO_FIELDS}
    if on_field:
        for k in res:
            on_field(k, "-")
    try:
        sess = open_session(m)
    except Exception:
        raise
    sess.cancel = cancel
    try:
        for key, _ in INFO_FIELDS:
            if cancel.is_set():
                break
            try:
                out = run_executor(sess, store.load_info(m, key))
            except ScanError:
                continue
            res[key] = out.text if out.ok and out.text else "-"
            if on_field:
                on_field(key, res[key])
    finally:
        sess.close()
    return res
