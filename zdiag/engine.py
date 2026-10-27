"""Execution engine shared by the "UDS诊断" and "ECU刷新" windows (no GUI code in here)."""
from __future__ import annotations
import os
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

from .bus import Bus, CanFrame
from .crc import CrcSpec, crc_bytes
from .flash import Segment, load_image
from .isotp import IsoTp, IsoTpError
from .project import (DiagConfig, Project, SUPPRESS_SIDS, fmt_hex, parse_hex, parse_id,
                      step_summary)
from .seedkey import SeedKeyError, SerialAwareKey, ZlgKeyDll, xor_demo_key
from .uds import NegativeResponse, NRC, UdsClient, UdsTimeout


class Cancelled(Exception):
    pass


class StepError(Exception):
    pass


# --------------------------------------------------------------------------- events
@dataclass
class LogEvent:
    ts: float
    kind: str                  # REQ RSP NRC INFO ERR OK CAN
    can_id: Optional[int]
    direction: str             # "TX" | "RX" | ""
    data: bytes
    text: str = ""

    def line(self, fmt: str = "hex") -> str:
        t = time.strftime("%H:%M:%S", time.localtime(self.ts)) + f".{int(self.ts * 1000) % 1000:03d}"
        cid = f"{self.can_id:X}" if self.can_id is not None else ""
        if self.data:
            body = fmt_hex(self.data) if fmt == "hex" else \
                "".join(chr(b) if 32 <= b < 127 else "." for b in self.data)
        else:
            body = ""
        desc = f"{body}  {self.text}".strip() if self.text else body
        return f"[{t}] {self.kind:<4} {self.direction:<2} {cid:>4} [{len(self.data)}] {desc}".rstrip()


class Hooks:
    """All members optional; the GUI plugs signal emitters in here."""
    def __init__(self) -> None:
        self.log: Callable[[LogEvent], None] = lambda e: None
        self.step: Callable[[int, str, str], None] = lambda i, status, detail: None
        self.progress: Callable[[int, int], None] = lambda done, total: None


class TapBus(Bus):
    """Transparent wrapper that reports raw frames of the diagnostic IDs."""
    def __init__(self, bus: Bus, report: Callable[[LogEvent], None], ids: set) -> None:
        self.bus, self.report, self.ids, self.enabled = bus, report, ids, False

    def send(self, frame: CanFrame) -> None:
        self.bus.send(frame)
        if self.enabled:
            self.report(LogEvent(time.time(), "CAN", frame.can_id, "TX", frame.data))

    def recv(self, timeout: float):
        fr = self.bus.recv(timeout)
        if fr is not None and self.enabled and fr.can_id in self.ids:
            self.report(LogEvent(time.time(), "CAN", fr.can_id, "RX", fr.data))
        return fr

    def flush(self) -> None:
        self.bus.flush()

    def close(self) -> None:
        self.bus.close()


# --------------------------------------------------------------------------- replies / matching
@dataclass
class Reply:
    ok: bool                   # positive (or suppressed) response received
    data: bytes = b""
    nrc: Optional[int] = None
    error: str = ""

    @property
    def negative(self) -> bool:
        return self.nrc is not None


def default_expect(pdu: bytes) -> str:
    return f"{(pdu[0] + 0x40) & 0xFF:02X}" if pdu else ""


def match_expect(expect: str, reply: Reply, any_negative: bool = False,
                 suppressed: bool = False) -> Tuple[bool, str]:
    """Check a reply against the expected PDU prefix ('xx' / '**' = wildcard byte)."""
    if reply.error:
        return False, reply.error
    toks = [t for t in re.split(r"[\s,]+", expect.strip()) if t]
    if not toks:
        return True, ""
    pattern = [None if t in ("xx", "XX", "**", "??") else int(t, 16) for t in toks]
    if pattern[0] == 0x7F:
        if not reply.negative:
            return False, "期望否定响应，但收到肯定响应"
        if any_negative:
            return True, ""
    elif reply.negative:
        return False, f"否定响应 NRC 0x{reply.nrc:02X} ({NRC.get(reply.nrc, '?')})"
    if not reply.data:
        return (True, "") if suppressed else (False, "无响应数据")
    if len(reply.data) < len(pattern):
        return False, f"响应过短: {fmt_hex(reply.data)}"
    for i, pb in enumerate(pattern):
        if pb is not None and reply.data[i] != pb:
            return False, f"响应不符: 期望 {expect.strip()}, 实际 {fmt_hex(reply.data[:len(pattern)])}"
    return True, ""


def render_template(text: str, ctx: Dict[str, bytes]) -> bytes:
    """'{addr} 00 {len}' -> bytes.  Unknown placeholders are an error."""
    out = b""
    for tok in re.split(r"[\s,]+", (text or "").strip()):
        if not tok:
            continue
        m = re.fullmatch(r"\{(\w+)\}", tok)
        if m:
            if m.group(1) not in ctx:
                raise StepError(f"未知占位符 {tok} (可用: {', '.join('{'+k+'}' for k in ctx)})")
            out += ctx[m.group(1)]
        else:
            out += parse_hex(tok)
    return out


def segments_for(path: str, base_addr: str = "0") -> List[Segment]:
    if not path:
        raise StepError("未选择文件")
    if not os.path.isfile(path):
        raise StepError(f"文件不存在: {path}")
    segs = load_image(path, int(base_addr or "0", 16))
    if not segs or not any(s.data for s in segs):
        raise StepError(f"文件为空: {path}")
    return segs


# --------------------------------------------------------------------------- session
class DiagSession:
    def __init__(self, bus: Bus, cfg: DiagConfig, hooks: Optional[Hooks] = None,
                 base_dir: str = ".", can_fd: bool = False, fd_dl: int = 64,
                 cancel: Optional[threading.Event] = None) -> None:
        self.cfg, self.hooks, self.base_dir = cfg, hooks or Hooks(), base_dir
        self.cancel = cancel or threading.Event()
        a, st = cfg.addr, cfg.settings
        self.tap = TapBus(bus, self.hooks.log, {a.phys, a.func, a.resp})
        self.tap.enabled = st.show_raw
        self.tp = IsoTp(
            self.tap, tx_id=a.phys, rx_id=a.resp, extended=a.extended,
            tx_dl=fd_dl if can_fd else 8, block_size=st.bs, st_min=st.st_min_ms,
            padding=st.padding if st.padding_enabled else None,
            n_bs=st.fc_timeout_ms / 1000.0, n_cr=max(st.fc_timeout_ms / 1000.0, 1.0),
            stmin_override=(st.override_stmin_ms / 1000.0) if st.override_stmin else None,
            auto_fc=st.auto_fc)
        self.uds = UdsClient(self.tp, p2=st.p2_ms / 1000.0, p2_star=st.p2star_ms / 1000.0)
        self.uds.auto_keepalive = False
        self.uds.suppress_wait = st.suppress_wait
        self.uds.hook = self._uds_hook
        self.verbose_transfer = False
        self.search_dirs: List[str] = [base_dir]      # where relative file / DLL paths are looked up
        self.last_download: Optional[Tuple[str, List[Segment]]] = None

    # -- logging
    def log(self, kind: str, text: str, can_id=None, direction: str = "", data: bytes = b"") -> None:
        self.hooks.log(LogEvent(time.time(), kind, can_id, direction, data, text))

    def _uds_hook(self, kind: str, payload: bytes) -> None:
        if payload and payload[0] in (0x36, 0x76) and not self.verbose_transfer:
            return
        a = self.cfg.addr
        if kind == "TX":
            self.log("REQ", "", self.tp.tx_id, "TX", payload)
        elif kind == "RX":
            self.log("RSP", "", a.resp, "RX", payload)
        else:
            nrc = payload[2] if len(payload) > 2 else 0
            self.log("NRC", f"NRC 0x{nrc:02X} {NRC.get(nrc, '')}", a.resp, "RX", payload)

    def close(self) -> None:
        self.uds.stop_keepalive()

    # -- keep alive (3E)
    def apply_keepalive(self) -> None:
        ka, a = self.cfg.keepalive, self.cfg.addr
        self.uds.stop_keepalive()
        if ka.enabled:
            self.uds.start_keepalive(ka.period_ms / 1000.0,
                                     a.func if ka.addr == "func" else a.phys,
                                     b"\x3E\x80" if ka.suppress else b"\x3E\x00")

    # -- single request
    def exchange(self, pdu: bytes, addr: str = "phys", suppress: bool = False) -> Reply:
        if not pdu:
            return Reply(False, error="请求为空")
        tx = self.cfg.addr.func if addr == "func" else self.cfg.addr.phys
        sup = suppress and pdu[0] in SUPPRESS_SIDS
        try:
            data = self.uds.request(pdu, suppress_positive=sup, tx_id=tx)
        except NegativeResponse as e:
            return Reply(False, bytes([0x7F, e.sid, e.nrc]), nrc=e.nrc)
        except (UdsTimeout, IsoTpError) as e:
            msg = str(e)
            self.log("ERR", msg)
            return Reply(False, error=msg)
        return Reply(True, data)

    def resolve(self, p: str) -> str:
        if not p or os.path.isabs(p):
            return p
        for d in self.search_dirs:
            cand = os.path.join(d, p)
            if os.path.exists(cand):
                return cand
        return os.path.join(self.base_dir, p)

    def check_cancel(self) -> None:
        if self.cancel.is_set():
            raise Cancelled()

    def sleep(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while True:
            self.check_cancel()
            left = end - time.perf_counter()
            if left <= 0:
                return
            time.sleep(min(left, 0.05))

    # ------------------------------------------------------------------ step runners
    def run_step(self, step: dict) -> str:
        """Run one step; returns a short detail string; raises StepError/Cancelled on failure."""
        fn = getattr(self, "_step_" + step["type"])
        try:
            return fn(step) or ""
        except (UdsTimeout, IsoTpError) as e:
            raise StepError(str(e))
        except NegativeResponse as e:
            raise StepError(str(e))
        except (SeedKeyError, ValueError, OSError) as e:
            raise StepError(str(e))

    def _expect(self, text: str, pdu: bytes) -> str:
        return text.strip() or default_expect(pdu)

    def _step_service(self, s: dict) -> str:
        pdu = parse_hex(s["pdu"])
        r = self.exchange(pdu, s.get("addr", "phys"), s.get("suppress", False))
        if s.get("check", True):
            ok, why = match_expect(self._expect(s.get("expect", ""), pdu), r,
                                   self.cfg.settings.any_negative,
                                   suppressed=s.get("suppress", False))
            if not ok:
                raise StepError(why)
        elif r.error:                                   # unchecked: only a missing answer fails
            if not (s.get("suppress") and "within" in r.error):
                raise StepError(r.error)
        return fmt_hex(r.data[:8])

    def _step_delay(self, s: dict) -> str:
        self.sleep(int(s["ms"]) / 1000.0)
        return f"{s['ms']} ms"

    def _key_fn(self, s: dict):
        dll = s.get("dll", "").strip()
        if not dll:
            raise StepError("未选择密钥 DLL")
        if dll.startswith("builtin:"):
            return xor_demo_key
        path = self.resolve(dll)
        if not os.path.isfile(path):
            raise StepError(f"密钥 DLL 不存在: {path}")
        wd = s.get("workdir", "").strip() or os.getcwd()
        kd = ZlgKeyDll(path, workdir=wd)
        if s.get("use_serial"):
            return SerialAwareKey(kd, id_map=self.cfg.id_map_int())
        return kd

    def _step_unlock(self, s: dict) -> str:
        level = int(s["level"])
        if level % 2 == 0:
            raise StepError("安全级别必须是奇数 (27 请求种子的子功能)")
        fresh = self.uds.security_access(self._key_fn(s), level)
        return "已解锁" if fresh else "已处于解锁状态(seed 全 0)"

    def _download(self, s: dict) -> str:
        st = self.cfg.settings
        segs = segments_for(self.resolve(s["file"]), s.get("base_addr", "0"))
        al, sl, dfi = int(s["addr_len"]), int(s["size_len"]), parse_hex(s.get("dfi", "00"))[0]
        total = sum(len(g.data) for g in segs)
        done = 0
        for seg in segs:
            self.check_cancel()
            max_len = self.uds.request_download(seg.addr, len(seg.data), al, sl, dfi)
            payload = max_len - 2
            if payload <= 0:
                raise StepError(f"ECU 返回的 maxNumberOfBlockLength 无效: {max_len}")
            seq = 1
            for off in range(0, len(seg.data), payload):
                self.check_cancel()
                chunk = seg.data[off:off + payload]
                for attempt in range(st.dl_retry + 1):
                    try:
                        self.uds.transfer_data(seq, chunk)
                        break
                    except (UdsTimeout, IsoTpError) as e:
                        if attempt >= st.dl_retry:
                            raise
                        self.log("ERR", f"36 块 {seq} 失败，重试 {attempt + 1}/{st.dl_retry}: {e}")
                seq = (seq + 1) & 0xFF
                done += len(chunk)
                self.hooks.progress(done, total)
                if st.block_interval_ms:
                    self.sleep(st.block_interval_ms / 1000.0)
            self.uds.request_transfer_exit()
        return f"{len(segs)} 个区域, {total} 字节"

    def _step_driver(self, s: dict) -> str:
        return self._download(s)

    def _step_download(self, s: dict) -> str:
        res = self._download(s)
        path = self.resolve(s["file"])
        self.last_download = (path, segments_for(path, s.get("base_addr", "0")))
        return res

    def _step_routine(self, s: dict) -> str:
        sub = parse_hex(s["sub"])[0]
        rid = parse_hex(s["rid"])
        if len(rid) != 2:
            raise StepError("例程标识符必须是 2 字节，如 DF FE")
        al, sl = int(s["addr_len"]), int(s["size_len"])
        use_crc = bool(s.get("use_crc"))
        tmpl = s.get("data", "")
        needs_file = use_crc or "{addr}" in tmpl or "{len}" in tmpl
        regions: List[Optional[Segment]] = [None]
        if needs_file:
            src = s.get("source", "").strip()
            if src:
                segs = segments_for(self.resolve(src))
            elif self.last_download:
                segs = self.last_download[1]
            else:
                raise StepError("该例程需要文件(CRC/地址/长度)，但此前没有执行过「文件下载」步骤，"
                                "也没有指定来源文件")
            if s.get("per_region", True):
                regions = list(segs)
            else:
                regions = [Segment(segs[0].addr, b"".join(g.data for g in segs))]
        spec = CrcSpec.from_dict(s.get("crc", {}))
        sent = []
        for reg in regions:
            self.check_cancel()
            ctx: Dict[str, bytes] = {}
            crc = b""
            if reg is not None:
                ctx["addr"] = reg.addr.to_bytes(al, "big")
                ctx["len"] = len(reg.data).to_bytes(sl, "big")
                if use_crc:
                    crc = crc_bytes(spec, reg.data, s.get("crc_order", "big"))
                    ctx["crc"] = crc
            body = render_template(tmpl, ctx)
            if use_crc and "{crc}" not in tmpl:
                body += crc
            pdu = bytes([0x31, sub]) + rid + body
            r = self.exchange(pdu, s.get("addr", "phys"), False)
            exp = s.get("expect", "").strip() or f"71 {sub:02X} {fmt_hex(rid)}"
            ok, why = match_expect(exp, r, self.cfg.settings.any_negative)
            if ok and s.get("status_ok", "").strip() and r.data:
                want = parse_hex(s["status_ok"])
                if r.data[4:4 + len(want)] != want:
                    ok, why = False, f"例程状态 {fmt_hex(r.data[4:])}，期望 {fmt_hex(want)}"
            if not ok:
                raise StepError(why)
            if use_crc and reg is not None:
                sent.append(f"0x{reg.addr:X}:{crc.hex().upper()}")
        return ("CRC " + ", ".join(sent)) if sent else "完成"

    # ------------------------------------------------------------------ plans
    def run_plan(self, steps: List[dict], repeat: int = 1, interval_s: float = 0.0) -> bool:
        """Run all steps `repeat` times. Returns True if every step passed."""
        self.apply_keepalive()
        try:
            for n in range(repeat):
                if repeat > 1:
                    self.log("INFO", f"===== 第 {n + 1}/{repeat} 次刷写 =====")
                self.last_download = None
                for i, step in enumerate(steps):
                    self.check_cancel()
                    self.hooks.step(i, "running", "")
                    self.hooks.progress(0, 0)
                    self.log("INFO", f"步骤 {i + 1}: {step.get('name', '')} {step_summary(step)}")
                    try:
                        detail = self.run_step(step)
                    except Cancelled:
                        self.hooks.step(i, "fail", "已取消")
                        raise
                    except StepError as e:
                        self.log("ERR", f"步骤 {i + 1} 失败: {e}")
                        self.hooks.step(i, "fail", str(e))
                        return False
                    self.hooks.step(i, "ok", detail)
                    self.log("OK", f"步骤 {i + 1} 完成 {detail}".strip())
                if n + 1 < repeat and interval_s > 0:
                    self.sleep(interval_s)
            return True
        except Cancelled:
            self.log("INFO", "已取消")
            return False
        finally:
            self.uds.stop_keepalive()


def _abs(base_dir: str, p: str) -> str:
    return p if os.path.isabs(p) else os.path.join(base_dir, p)


def preflight(steps: List[dict], base_dir: str = ".") -> List[str]:
    """Static checks before touching the ECU. Returns human-readable problems (empty = ok)."""
    out: List[str] = []
    seen_download = False
    for i, s in enumerate(steps, 1):
        tag = f"步骤{i} {s.get('name', '')}"
        t = s.get("type")
        try:
            if t == "service":
                if not parse_hex(s.get("pdu", "")):
                    out.append(f"{tag}: 请求PDU为空")
            elif t == "delay":
                if int(s.get("ms", 0)) < 0:
                    out.append(f"{tag}: 延时无效")
            elif t == "unlock":
                if int(s.get("level", 1)) % 2 == 0:
                    out.append(f"{tag}: 安全级别必须是奇数")
                dll = s.get("dll", "").strip()
                if not dll:
                    out.append(f"{tag}: 未选择密钥 DLL")
                elif not dll.startswith("builtin:") and not os.path.isfile(_abs(base_dir, dll)):
                    out.append(f"{tag}: 密钥 DLL 不存在: {_abs(base_dir, dll)}")
            elif t in ("driver", "download"):
                f = s.get("file", "").strip()
                if not f:
                    out.append(f"{tag}: 未选择文件")
                else:
                    segments_for(_abs(base_dir, f), s.get("base_addr", "0"))
                    seen_download = seen_download or t == "download"
            elif t == "routine":
                if len(parse_hex(s.get("rid", ""))) != 2:
                    out.append(f"{tag}: 例程标识符必须是 2 字节")
                needs = s.get("use_crc") or "{addr}" in s.get("data", "") or "{len}" in s.get("data", "")
                src = s.get("source", "").strip()
                if needs and src:
                    segments_for(_abs(base_dir, src))
                elif needs and not seen_download:
                    out.append(f"{tag}: 需要文件，但前面没有「文件下载」步骤，也未指定数据来源文件")
        except (StepError, ValueError, OSError) as e:
            out.append(f"{tag}: {e}")
    return out
