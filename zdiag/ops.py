"""Operations layer (no Qt): everything the host can do, as plain methods taking / returning JSON-able data.

`Ops(ctx)` works on an AppContext, so the GUI, the headless daemon and the agent port share ONE device manager,
settings and monitor.  Every method either returns a dict or raises OpsError (a message meant for the user / agent).
The RPC server (zdiag.rpc) exposes exactly the methods listed in METHODS."""
import copy
import dataclasses
import os
import re
import threading
import time
from dataclasses import asdict, fields
from typing import Any, Dict, List, Optional

from .busstats import ChannelCfg
from .devmgr import ConnError, channel_count, is_fd_device
from .engine import Hooks, LogEvent
from .uds import NRC as NRC_TEXT
from .project import (DEVICES, SIM_DEVICE, Addressing, DiagConfig, fmt_hex, new_step, parse_hex, parse_id)
from .scanlib import (INFO_FIELDS, Executor, ScanError, ScanStore, cfg_for, default_skip, function_paths, render_result,
                      run_executor)
from .sendlist import FD_LENGTHS, SendItem, run_send
from . import cft as cftmod
from . import lin as linmod

MAX_FRAMES = 100000                 # one frame_send call
MAX_BLOCKING_S = 120.0              # a blocking call (frame_send) never runs longer than this
LOG_KEEP = 40                       # LogEvents returned with a diagnostic call


class OpsError(Exception):
    pass


def _hex(text: Any, what: str = "数据") -> bytes:
    try:
        return parse_hex(str(text))
    except ValueError as e:
        raise OpsError(f"{what}无效: {e}")


def _int(v: Any, what: str, lo: int, hi: int) -> int:
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise OpsError(f"{what}应为整数")
    if not lo <= n <= hi:
        raise OpsError(f"{what}应在 {lo}~{hi} 之间")
    return n


def _safe_name(name: str, what: str = "文件名") -> str:
    n = (name or "").strip()
    if not n or any(c in n for c in '\\/:*?"<>|') or ".." in n:
        raise OpsError(f"{what}无效: {name!r}（不能为空，不能含 \\ / : * ? \" < > | ..）")
    return n


def _typed_merge(obj, values: Dict[str, Any], section: str, readonly: tuple = ()) -> None:
    """Set dataclass fields from `values` in place; unknown names / wrong types raise OpsError (nothing is changed)."""
    names = {f.name: f for f in fields(obj)}
    staged = {}
    for k, v in (values or {}).items():
        if k not in names:
            raise OpsError(f"{section} 没有设置项「{k}」。可用: {', '.join(names)}")
        if k in readonly:
            raise OpsError(f"{section}.{k} 不能在这里修改")
        cur = getattr(obj, k)
        if isinstance(cur, bool):
            if not isinstance(v, bool):
                raise OpsError(f"{section}.{k} 应为 true/false")
        elif isinstance(cur, int):
            if isinstance(v, bool) or not isinstance(v, int):
                raise OpsError(f"{section}.{k} 应为整数")
            if v < 0:
                raise OpsError(f"{section}.{k} 不能为负数")
        elif isinstance(cur, str):
            if not isinstance(v, str):
                raise OpsError(f"{section}.{k} 应为文本")
        staged[k] = v
    for k, v in staged.items():
        setattr(obj, k, v)


class _Token:
    """Channel owner token of an Ops instance; the device manager calls abort() when the channel is stopped / the device closed."""

    def __init__(self, ops: "Ops", ch: int) -> None:
        self.ops, self.ch = ops, ch

    def abort(self) -> None:
        self.ops.abort_channel(self.ch)


class Ops:
    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.lock = threading.RLock()
        self.store = ScanStore(ctx.scan_dir)
        self.store.ensure_defaults()
        self._owner: Dict[int, object] = {}                    # channel -> owner token while something of ours uses it
        self._uses: Dict[int, int] = {}
        self._senders: Dict[str, dict] = {}                    # key -> {"ev","thread","ch","res","item"}
        self._finished: Dict[str, dict] = {}                   # key -> summary of a sender that has ended
        self._keepalive: Dict[int, Any] = {}                   # channel -> DiagSession holding a 3E keep-alive
        self._job: Optional[dict] = None                       # the running / last 功能 started with function_start
        self._ft: Dict[str, Any] = {}                         # file-transfer jobs by role ("send" / "receive")
        self._lin: Optional[dict] = None                       # the open LIN master: {"master","kind","channel","baud"}

    # ------------------------------------------------------------------ channel ownership
    def _hold(self, ch: int):
        """Acquire the channel for us (shared with our other users of it); returns the owner token."""
        with self.lock:
            tok = self._owner.get(ch)
            if tok is None:
                tok = self._owner[ch] = _Token(self, ch)
            self._uses[ch] = self._uses.get(ch, 0) + 1
            return tok

    def _drop(self, ch: int) -> None:
        with self.lock:
            self._uses[ch] = self._uses.get(ch, 1) - 1
            if self._uses[ch] <= 0:
                self._uses.pop(ch, None)
                tok = self._owner.pop(ch, None)
                if tok is not None:
                    self.ctx.dev.release(tok)

    def abort_channel(self, ch: int) -> None:
        """Ask everything of ours that uses channel `ch` to stop (flags only, never blocks; the threads wind down by themselves)."""
        for j in list(self._ft.values()):
            if ch in getattr(j, "channels", ()):
                if getattr(j.engine.cfg, "persist", False):            # a transfer that reconnects by itself only lets go of the channel
                    for i, c in enumerate(j.channels):
                        if c == ch and i < len(j.engine.links):
                            j.engine.links[i].detach("通道被关闭")
                else:
                    j.engine.stop()
        for s in list(self._senders.values()):
            if s["ch"] == ch:
                s["ev"].set()
        j = self._job
        if j is not None and j.get("ch") == ch and "sess" in j:
            j["sess"].cancel.set()
        sess = self._keepalive.pop(ch, None)
        if sess is not None:
            sess.cancel.set()
            try:
                sess.close()
            except Exception:
                pass
            self._drop(ch)

    def _bus(self, ch: int):
        """(bus, token) of a started channel; the caller must _drop(ch) afterwards."""
        tok = self._hold(ch)
        try:
            return self.ctx.dev.acquire(ch, tok), tok
        except ConnError as e:
            self._drop(ch)
            raise OpsError(str(e))

    # ------------------------------------------------------------------ device / channels
    def status(self) -> Dict[str, Any]:
        d = self.ctx.dev
        chans = []
        for n in d.started():
            c = d.cfg_of(n)
            st = d.stats_of(n)
            rx, tx, err = st.snapshot() if st else (0, 0, 0)
            chans.append({"channel": n, "config": asdict(c), "summary": c.summary(), "rx": rx, "tx": tx, "errors": err,
                          "bus_load_percent": None if st is None or st.load is None else round(float(st.load), 1),
                          "in_use_by_window": n in d.owners and n not in self._owner})
        return {"device": d.name, "index": d.index, "is_open": d.is_open, "channels": chans,
                "senders": self.cyclic_list()["senders"], "recording": self.ctx.dev.tap.recorder is not None,
                "scan_dir": self.ctx.scan_dir}

    def device_list(self) -> Dict[str, Any]:
        return {"devices": list(DEVICES), "simulator": SIM_DEVICE,
                "channel_counts": {n: channel_count(n) for n in DEVICES}, "current": self.status()}

    def device_open(self, name: str, index: int = 0) -> Dict[str, Any]:
        try:
            if self.ctx.dev.is_open and self.ctx.dev.name == name:
                return {"message": "设备已打开", **self.status()}
            self.ctx.dev.open(str(name), _int(index, "设备索引", 0, 15))
        except ConnError as e:
            raise OpsError(str(e))
        self.ctx.devsel.update({"device": name, "index": index})
        return {"message": "已打开", **self.status()}

    def device_close(self) -> Dict[str, Any]:
        self._stop_all_ours()
        try:
            self.ctx.dev.close()
        except ConnError as e:
            raise OpsError(str(e))
        return {"message": "已关闭", **self.status()}

    def channel_start(self, channel: int, protocol: str = "CAN", bitrate_kbps: float = 500.0, data_kbps: float = 2000.0,
                      fd_accel: bool = True, listen_only: bool = False, terminator: bool = True) -> Dict[str, Any]:
        d = self.ctx.dev
        if not d.is_open:
            raise OpsError("请先 device_open 打开设备")
        n = _int(channel, "通道号", 0, 15)
        if n in d.started():
            c = d.cfg_of(n)
            return {"message": f"通道{n} 已启动（沿用当前配置 {c.summary()}）", **self.status()}
        if protocol not in ("CAN", "CAN FD"):
            raise OpsError('protocol 应为 "CAN" 或 "CAN FD"')
        cfg = ChannelCfg(protocol=protocol, bitrate_kbps=float(bitrate_kbps), data_kbps=float(data_kbps),
                         fd_accel=bool(fd_accel), listen_only=bool(listen_only), terminator=bool(terminator))
        try:
            d.start_channel(n, cfg, self.ctx.diag_cfg.addr, self.ctx.channel.sim_profile, self.ctx.diag_cfg.settings.rx_padding)
        except ConnError as e:
            raise OpsError(str(e))
        self.ctx.chan_cfgs[n] = cfg
        return {"message": f"通道{n} 已启动 {cfg.summary()}", **self.status()}

    def channel_stop(self, channel: int) -> Dict[str, Any]:
        n = _int(channel, "通道号", 0, 15)
        self._stop_channel_users(n)
        try:
            self.ctx.dev.stop_channel(n)
        except ConnError as e:
            raise OpsError(str(e))
        return {"message": f"通道{n} 已停止", **self.status()}

    def _stop_channel_users(self, n: int) -> None:
        for k in [k for k, s in self._senders.items() if s["ch"] == n]:
            self._stop_sender(k)
        self.keepalive_stop(n)

    def _stop_all_ours(self) -> None:
        self.ft_stop(wait=2.0)
        self.lin_close()
        for k in list(self._senders):
            self._stop_sender(k)
        for n in list(self._keepalive):
            self.keepalive_stop(n)

    # ------------------------------------------------------------------ diagnostics
    def _cfg(self, module: str = "", phys: str = "", resp: str = "", func: str = "", extended: Optional[bool] = None) -> DiagConfig:
        if module:
            m = next((m for m in self.store.modules() if m.name == module), None)
            if m is None:
                raise OpsError(f"找不到软件模块「{module}」，可用: {', '.join(x.name for x in self.store.modules())}")
            cfg = cfg_for(self.ctx.diag_cfg, m)
        else:
            cfg = copy.deepcopy(self.ctx.diag_cfg)
        try:
            if phys:
                cfg.addr.phys = parse_id(phys)
            if resp:
                cfg.addr.resp = parse_id(resp)
            if func:
                cfg.addr.func = parse_id(func)
        except ValueError as e:
            raise OpsError(f"CAN ID 无效: {e}")
        if extended is not None:
            cfg.addr.extended = bool(extended)
        return cfg

    def _session(self, channel: int, cfg: DiagConfig, log: List[LogEvent], hooks: Optional[Hooks] = None, all_log: bool = False):
        n = _int(channel, "通道号", 0, 15)
        tok = self._hold(n)
        hooks = hooks or Hooks()
        hooks.log = (lambda e: log.append(e)) if all_log else (lambda e: log.append(e) if e.core else None)
        try:
            sess = self.ctx.open_session(cfg, dataclasses.replace(self.ctx.channel, channel=n), tok, hooks)
        except ConnError as e:
            self._drop(n)
            raise OpsError(str(e))
        return sess, n

    @staticmethod
    def _log_lines(log: List[LogEvent]) -> List[str]:
        return [e.line() for e in log[-LOG_KEEP:]]

    def uds_request(self, channel: int, pdu: str, module: str = "", phys: str = "", resp: str = "", func: str = "",
                    extended: Optional[bool] = None, addr: str = "phys", suppress: bool = False,
                    parse: str = "hex", skip: int = -1) -> Dict[str, Any]:
        """One UDS request (hex PDU) -> response. Positive: `data` = full response, `value` = payload after the echoed
        header rendered per `parse` (hex | ascii | dec). Negative: `nrc` / `nrc_text`. No answer: `error`."""
        req = _hex(pdu, "请求报文")
        if not req:
            raise OpsError("请求报文为空")
        if parse not in ("hex", "ascii", "dec"):
            raise OpsError("parse 应为 hex / ascii / dec")
        if addr not in ("phys", "func"):
            raise OpsError('addr 应为 "phys" 或 "func"')
        cfg = self._cfg(module, phys, resp, func, extended)
        log: List[LogEvent] = []
        sess, n = self._session(channel, cfg, log)
        try:
            r = sess.exchange(req, addr, suppress)
        finally:
            sess.close()
            self._drop(n)
        out: Dict[str, Any] = {"ok": r.ok, "request": fmt_hex(req), "channel": n,
                               "phys": f"{cfg.addr.phys:X}", "resp": f"{cfg.addr.resp:X}", "log": self._log_lines(log)}
        if r.ok:
            data = bytes(r.data)
            s = default_skip(req) if skip < 0 else skip
            out.update(response=fmt_hex(data), data=fmt_hex(data), value=render_result(data[s:], parse))
            if suppress and not data:
                out["note"] = "已抑制肯定响应，没有回复属于正常"
        elif r.negative:
            out.update(response=fmt_hex(r.data), nrc=f"{r.nrc:02X}", nrc_text=NRC_TEXT.get(r.nrc, ""),
                       error=f"否定响应 NRC 0x{r.nrc:02X} {NRC_TEXT.get(r.nrc, '')}".strip())
        else:
            out["error"] = r.error or "无响应"
        return out

    def did_read(self, channel: int, did: str, module: str = "", parse: str = "hex", **kw) -> Dict[str, Any]:
        d = _hex(did, "DID")
        if len(d) != 2:
            raise OpsError("DID 应为 2 字节，如 F190")
        return self.uds_request(channel, "22 " + fmt_hex(d), module=module, parse=parse, **kw)

    def did_write(self, channel: int, did: str, data: str, module: str = "", **kw) -> Dict[str, Any]:
        d = _hex(did, "DID")
        if len(d) != 2:
            raise OpsError("DID 应为 2 字节，如 F190")
        payload = _hex(data, "写入数据")
        if not payload:
            raise OpsError("写入数据为空")
        return self.uds_request(channel, "2E " + fmt_hex(d + payload), module=module, **kw)

    def dtc_read(self, channel: int, module: str = "", mask: str = "08", **kw) -> Dict[str, Any]:
        """19 02 <mask>: DTCs by status mask; `dtcs` lists 'XXXXXX status' entries."""
        m = _hex(mask, "状态掩码")
        if len(m) != 1:
            raise OpsError("状态掩码应为 1 字节，如 08")
        r = self.uds_request(channel, "19 02 " + fmt_hex(m), module=module, **kw)
        if r.get("ok"):
            body = parse_hex(r["data"])[3:]                       # 59 02 <availabilityMask> then 4-byte records
            r["dtcs"] = [f"{body[i]:02X}{body[i + 1]:02X}{body[i + 2]:02X} status={body[i + 3]:02X}"
                         for i in range(0, len(body) - 3, 4)]
            r["count"] = len(r["dtcs"])
        return r

    def dtc_clear(self, channel: int, module: str = "", group: str = "FF FF FF", **kw) -> Dict[str, Any]:
        g = _hex(group, "DTC 组")
        if len(g) != 3:
            raise OpsError("DTC 组应为 3 字节，如 FFFFFF")
        return self.uds_request(channel, "14 " + fmt_hex(g), module=module, **kw)

    def ecu_reset(self, channel: int, module: str = "", kind: str = "01", **kw) -> Dict[str, Any]:
        k = _hex(kind, "复位类型")
        if len(k) != 1:
            raise OpsError("复位类型应为 1 字节，如 01")
        return self.uds_request(channel, "11 " + fmt_hex(k), module=module, **kw)

    def session_control(self, channel: int, session: str = "03", module: str = "", **kw) -> Dict[str, Any]:
        s = _hex(session, "会话类型")
        if len(s) != 1:
            raise OpsError("会话类型应为 1 字节: 01 默认 / 02 编程 / 03 扩展")
        return self.uds_request(channel, "10 " + fmt_hex(s), module=module, **kw)

    def security_unlock(self, channel: int, dll: str = "", level: int = 1, module: str = "", **kw) -> Dict[str, Any]:
        """27 seed / key unlock. The key DLL is the global one (设置 → 安全访问 / settings_set security.key_dll);
        `dll` (path, or 'builtin:xor' for the demo algorithm) overrides it for this call only."""
        if not dll and not self.ctx.security.key_dll.strip():
            raise OpsError("还没有配置密钥 DLL：先 settings_set(section='security', values={'key_dll': '路径'})，或本次传 dll；演示算法用 builtin:xor")
        lvl = _int(level, "安全级别", 1, 0x7D)
        if lvl % 2 == 0:
            raise OpsError("安全级别应为奇数 (27 请求种子的子功能)")
        cfg = self._cfg(module, **{k: v for k, v in kw.items() if k in ("phys", "resp", "func", "extended")})
        log: List[LogEvent] = []
        sess, n = self._session(channel, cfg, log)
        try:
            from .engine import StepError
            try:
                detail = sess.run_step(new_step("unlock", level=lvl, dll=dll or ""))
                return {"ok": True, "message": detail, "log": self._log_lines(log)}
            except StepError as e:
                return {"ok": False, "error": str(e), "log": self._log_lines(log)}
        finally:
            sess.close()
            self._drop(n)

    def keepalive_start(self, channel: int, module: str = "", period_ms: int = 2000, addr: str = "func",
                        suppress: bool = True) -> Dict[str, Any]:
        """Keep sending Tester Present (3E) on the channel until keepalive_stop."""
        n = _int(channel, "通道号", 0, 15)
        if n in self._keepalive:
            raise OpsError(f"通道{n} 已在发送 Tester Present")
        cfg = self._cfg(module)
        cfg.keepalive.enabled, cfg.keepalive.period_ms = True, _int(period_ms, "周期", 20, 60000)
        cfg.keepalive.addr, cfg.keepalive.suppress = ("func" if addr == "func" else "phys"), bool(suppress)
        sess, n = self._session(n, cfg, [])
        sess.apply_keepalive()
        self._keepalive[n] = sess
        return {"message": f"通道{n} 开始发送 Tester Present，每 {cfg.keepalive.period_ms} ms"}

    def keepalive_stop(self, channel: int = -1) -> Dict[str, Any]:
        stopped = []
        for n in [c for c in list(self._keepalive) if channel < 0 or c == channel]:
            sess = self._keepalive.pop(n)
            sess.close()
            self._drop(n)
            stopped.append(n)
        return {"message": "已停止" if stopped else "没有在发送 Tester Present", "stopped": stopped}

    # ------------------------------------------------------------------ scan_data modules / functions
    def modules(self) -> Dict[str, Any]:
        return {"modules": [{"name": m.name, "phys": f"{m.addr().phys:X}", "resp": f"{m.addr().resp:X}",
                             "functions": function_paths(self.store, m.name)} for m in self.store.modules()],
                "scan_fields": [{"key": k, "label": v} for k, v in INFO_FIELDS]}

    def _module(self, name: str):
        for m in self.store.modules():
            if m.name == name:
                return m
        raise OpsError(f"找不到软件模块「{name}」，可用: {', '.join(x.name for x in self.store.modules())}")

    def scan(self, channel: int, module: str, fields: Optional[List[str]] = None) -> Dict[str, Any]:
        """软件扫描 of one module: software code / version / hardware version / DTC count."""
        m = self._module(module)
        want = list(fields) if fields else [k for k, _ in INFO_FIELDS]
        bad = [k for k in want if k not in dict(INFO_FIELDS)]
        if bad:
            raise OpsError(f"未知字段 {bad}，可用: {[k for k, _ in INFO_FIELDS]}")
        log: List[LogEvent] = []
        sess, n = self._session(channel, cfg_for(self.ctx.diag_cfg, m), log)
        res: Dict[str, Any] = {}
        try:
            for key, label in INFO_FIELDS:
                if key not in want:
                    continue
                try:
                    out = run_executor(sess, self.store.load_info(m, key))
                except ScanError as e:
                    res[key] = {"label": label, "ok": False, "value": "-", "error": str(e)}
                    continue
                ok = out.ok and bool(out.text)
                res[key] = {"label": label, "ok": ok, "value": out.text if ok else "-", **({} if ok else {"error": out.error or "无数据"})}
        finally:
            sess.close()
            self._drop(n)
        return {"ok": any(v["ok"] for v in res.values()), "module": module, "fields": res, "log": self._log_lines(log)}

    def function_run(self, channel: int, module: str, path: str) -> Dict[str, Any]:
        """Run a saved 功能 of a module (e.g. '故障检测') and return its result text."""
        m = self._module(module)
        p = os.path.join(self.store.func_dir(m), *str(path).replace("\\", "/").split("/")) + ".json"
        if ".." in str(path) or not os.path.isfile(p):
            raise OpsError(f"模块「{module}」里没有功能「{path}」，可用: {', '.join(function_paths(self.store, module))}")
        log: List[LogEvent] = []
        sess, n = self._session(channel, cfg_for(self.ctx.diag_cfg, m), log)
        try:
            out = run_executor(sess, Executor.load(p))
        finally:
            sess.close()
            self._drop(n)
        return {"ok": out.ok, "value": out.text, "raw": fmt_hex(out.raw), "error": out.error, "log": self._log_lines(log)}

    def function_start(self, channel: int, module: str, path: str) -> Dict[str, Any]:
        """Run a saved 功能 in the background (for long ones such as flashing); poll function_status, stop with function_stop."""
        with self.lock:
            if self._job and self._job["thread"].is_alive():
                raise OpsError("已有功能在执行，请先 function_stop 或等它结束")
        m = self._module(module)
        p = os.path.join(self.store.func_dir(m), *str(path).replace("\\", "/").split("/")) + ".json"
        if ".." in str(path) or not os.path.isfile(p):
            raise OpsError(f"模块「{module}」里没有功能「{path}」，可用: {', '.join(function_paths(self.store, module))}")
        ex = Executor.load(p)
        from .engine import preflight
        problems = preflight(list(ex.steps), self.ctx.dir, self.ctx.security.key_path(self.ctx.dir))
        if problems:
            return {"started": False, "message": "功能还不完整，未执行", "problems": problems}
        log: List[LogEvent] = []
        job: Dict[str, Any] = {"module": module, "path": path, "steps": {}, "progress": (0, 0), "log": log, "ok": None, "error": "",
                               "started": time.time(), "names": [x.get("name", "") for x in ex.steps]}
        hooks = Hooks()
        hooks.step = lambda i, st, d: job["steps"].__setitem__(i, (st, d))
        hooks.progress = lambda done, total: job.__setitem__("progress", (done, total)) if total else None   # (0, 0) = "no bar"
        sess, n = self._session(channel, cfg_for(self.ctx.diag_cfg, m), log, hooks, all_log=True)
        job["sess"], job["ch"] = sess, n

        def go() -> None:
            try:
                out = run_executor(sess, ex)
                job["ok"], job["error"] = out.ok, out.error
                job["value"] = out.text
            except Exception as e:
                job["ok"], job["error"] = False, f"{type(e).__name__}: {e}"
            finally:
                sess.close()
                self._drop(n)

        t = threading.Thread(target=go, name="ops-function", daemon=True)
        job["thread"] = t
        self._job = job
        t.start()
        return {"started": True, "message": f"「{module}/{path}」已开始，用 function_status 查看进度"}

    def function_status(self) -> Dict[str, Any]:
        j = self._job
        if j is None:
            return {"state": "none", "message": "还没有执行过功能"}
        running = j["thread"].is_alive()
        steps = [{"index": i + 1, "name": j["names"][i] if i < len(j["names"]) else "", "state": st, "detail": d[:300]}
                 for i, (st, d) in sorted(j["steps"].items())]
        done, total = j["progress"]
        return {"state": "running" if running else "finished", "module": j["module"], "path": j["path"],
                "ok": None if running else bool(j["ok"]), "error": "" if running else j["error"], "value": j.get("value", ""),
                "progress": {"done": done, "total": total}, "steps": steps, "log": self._log_lines(j["log"]),
                "elapsed_s": round(time.time() - j["started"], 1)}

    def function_stop(self, wait: float = 15.0) -> Dict[str, Any]:
        j = self._job
        if j is None or not j["thread"].is_alive():
            return {"message": "没有正在执行的功能"}
        j["sess"].cancel.set()
        j["thread"].join(timeout=wait)
        return {"message": "已请求停止", **self.function_status()}

    # ------------------------------------------------------------------ raw frames
    def _item(self, can_id: str, data: str, extended: bool, fd: bool, remote: bool, count: int, interval_ms: int) -> SendItem:
        try:
            cid = parse_id(can_id)
        except ValueError as e:
            raise OpsError(f"CAN ID 无效: {e}")
        payload = _hex(data, "数据") if data else b""
        length = len(payload) if not fd else next((x for x in FD_LENGTHS if x >= len(payload)), 64)
        it = SendItem(can_id=cid, extended=bool(extended), remote=bool(remote), length=length,
                      data=payload, count=int(count), interval_ms=int(interval_ms))
        try:
            it.validate(bool(fd))
        except ValueError as e:
            raise OpsError(str(e))
        return it

    def _fd_flags(self, ch: int, fd: bool):
        cfg = self.ctx.dev.cfg_of(ch)
        if fd and not (cfg and cfg.is_fd):
            raise OpsError(f"通道{ch} 不是 CAN FD 模式，不能发送 FD 帧")
        return bool(fd and cfg and cfg.fd_accel)

    def frame_send(self, channel: int, can_id: str, data: str = "", extended: bool = False, fd: bool = False,
                   remote: bool = False, count: int = 1, interval_ms: int = 0) -> Dict[str, Any]:
        """Send a raw CAN / CAN FD frame `count` times (blocking, at most 120 s). For endless sending use cyclic_start."""
        n = _int(channel, "通道号", 0, 15)
        c = _int(count, "次数", 1, MAX_FRAMES)
        iv = _int(interval_ms, "间隔(ms)", 0, 600000)
        if c > 1 and (c - 1) * iv / 1000.0 > MAX_BLOCKING_S:
            raise OpsError(f"发送耗时超过 {MAX_BLOCKING_S:g} 秒，请减小次数/间隔，或用 cyclic_start")
        it = self._item(can_id, data, extended, fd, remote, c, iv)
        brs = self._fd_flags(n, fd)
        bus, _ = self._bus(n)
        try:
            r = run_send(bus, [it], fd=bool(fd), brs=brs, stop_on_fail=True)
        finally:
            self._drop(n)
        return {"ok": r.ok, "sent": r.sent, "failed": r.failed, "elapsed_s": round(r.elapsed, 3)}

    def cyclic_start(self, channel: int, key: str, can_id: str, data: str = "", period_ms: int = 100, extended: bool = False,
                     fd: bool = False, count: int = -1) -> Dict[str, Any]:
        """Start sending a frame every period_ms in the background (count -1 = until cyclic_stop)."""
        k = _safe_name(key, "名称(key)")
        n = _int(channel, "通道号", 0, 15)
        per = _int(period_ms, "周期(ms)", 1, 600000)
        cnt = _int(count, "次数", -1, 10 ** 9)
        if cnt == 0:
            raise OpsError("次数不能为 0（-1 表示一直发送）")
        self.cyclic_list()                                           # reap senders that ended
        with self.lock:
            if k in self._senders:
                raise OpsError(f"周期发送「{k}」已经在运行")
            self._finished.pop(k, None)
        it = self._item(can_id, data, extended, fd, False, cnt, per)
        brs = self._fd_flags(n, fd)
        bus, _ = self._bus(n)
        ev = threading.Event()
        info = {"ev": ev, "ch": n, "res": None, "item": f"{it.can_id:X} [{it.data.hex(' ').upper()}] /{per}ms", "err": ""}

        def go() -> None:
            try:
                info["res"] = run_send(bus, [it], fd=bool(fd), brs=brs, cancel=ev, stop_on_fail=True)
            except Exception as e:
                info["err"] = str(e)

        t = threading.Thread(target=go, name="ops-send", daemon=True)
        info["thread"] = t
        with self.lock:
            self._senders[k] = info
        t.start()
        return {"message": f"「{k}」已开始: ID {info['item']}"}

    def _stop_sender(self, key: str) -> Dict[str, Any]:
        with self.lock:
            info = self._senders.pop(key, None)
        if info is None:
            return {}
        info["ev"].set()
        info["thread"].join(timeout=3)
        self._drop(info["ch"])
        r = info.get("res")
        summary = {"key": key, "sent": r.sent if r else 0, "failed": r.failed if r else 0, **({"error": info["err"]} if info["err"] else {})}
        self._finished[key] = summary
        return summary

    def cyclic_stop(self, key: str = "") -> Dict[str, Any]:
        keys = [k for k in list(self._senders) if not key or k == key]
        if not keys:
            raise OpsError("没有正在运行的周期发送" + (f"「{key}」" if key else ""))
        return {"stopped": [self._stop_sender(k) for k in keys]}

    def cyclic_list(self) -> Dict[str, Any]:
        for k in [k for k, s in list(self._senders.items()) if not s["thread"].is_alive()]:
            self._stop_sender(k)                                    # finished by itself (count reached): release the channel
        out = [{"key": k, "channel": s["ch"], "frame": s["item"], "running": True} for k, s in list(self._senders.items())]
        out += [{"key": k, "running": False, **v} for k, v in self._finished.items() if k not in self._senders]
        return {"senders": out}

    # ------------------------------------------------------------------ monitor
    @staticmethod
    def _row(r, t0: float) -> Dict[str, Any]:
        return {"seq": r.seq, "t_s": round(r.ts - t0, 6), "channel": r.ch, "dir": r.dir, "id": r.id_text, "type": r.type_text,
                "len": 0 if r.err else len(r.data), "data": r.data_text, "count": r.count}

    def _filter(self, rows, ids: str, channel: int, direction: str):
        want = set()
        for tok in re.split(r"[\s,;]+", ids or ""):
            if tok:
                try:
                    want.add(parse_id(tok))
                except ValueError:
                    raise OpsError(f"过滤的 CAN ID 无效: {tok!r}")
        out = []
        for r in rows:
            if want and r.can_id not in want:
                continue
            if channel >= 0 and r.ch != channel:
                continue
            if direction and r.dir != direction.upper():
                continue
            out.append(r)
        return out

    def monitor_trace(self, count: int = 100, ids: str = "", channel: int = -1, direction: str = "") -> Dict[str, Any]:
        """The last `count` frames (chronological) from the shared monitor. ids: 'ID1 ID2' hex filter; direction TX|RX."""
        tap = self.ctx.dev.tap
        rows = self._filter(tap.trace(_int(count, "数量", 1, tap.RING)), ids, channel, direction)
        return {"total_frames": tap.total, "errors": tap.errors, "returned": len(rows), "frames": [self._row(r, tap.t0) for r in rows]}

    def monitor_snapshot(self, ids: str = "", channel: int = -1) -> Dict[str, Any]:
        """One line per (channel, direction, ID): last data, count and period."""
        tap = self.ctx.dev.tap
        rows = self._filter(tap.snapshot(), ids, channel, "")
        return {"total_frames": tap.total, "errors": tap.errors,
                "ids": [{**self._row(r, tap.t0), "period_ms": round(r.dt_id * 1000, 3)} for r in rows]}

    def monitor_capture(self, seconds: float = 1.0, ids: str = "", channel: int = -1, direction: str = "",
                        limit: int = 500) -> Dict[str, Any]:
        """Wait `seconds` (<= 60) and return the frames that arrived meanwhile."""
        s = float(seconds)
        if not 0 < s <= 60:
            raise OpsError("seconds 应在 0~60 之间")
        tap = self.ctx.dev.tap
        t0 = tap.total
        time.sleep(s)
        new = tap.total - t0
        rows = [r for r in tap.trace(tap.RING)][-max(new, 0):] if new > 0 else []
        rows = self._filter(rows, ids, channel, direction)
        lim = _int(limit, "limit", 1, 5000)
        return {"seconds": s, "new_frames": new, "returned": min(len(rows), lim), "truncated": len(rows) > lim,
                "frames": [self._row(r, tap.t0) for r in rows[:lim]]}

    def monitor_clear(self) -> Dict[str, Any]:
        self.ctx.dev.tap.reset()
        return {"message": "监视数据已清空"}

    def record_start(self, name: str = "") -> Dict[str, Any]:
        """Real-time save of all frames to <log folder>/<name>.csv (size-rotated per the log settings)."""
        folder = self.ctx.logcfg.folder(self.ctx.dir)
        os.makedirs(folder, exist_ok=True)
        n = _safe_name(name or time.strftime("agent_%Y%m%d_%H%M%S"))
        path = os.path.join(folder, n if n.lower().endswith(".csv") else n + ".csv")
        self.ctx.dev.tap.start_record(path, self.ctx.logcfg.max_bytes)
        return {"message": "开始录制", "path": path}

    def record_stop(self) -> Dict[str, Any]:
        rec = self.ctx.dev.tap.stop_record()
        return {"message": "已停止录制" if rec else "当前没有在录制"}

    # ------------------------------------------------------------------ settings
    def settings_get(self, section: str = "") -> Dict[str, Any]:
        ctx = self.ctx
        all_ = {"diag": ctx.diag_cfg.to_dict(), "security": ctx.security.to_dict(), "log": asdict(ctx.logcfg),
                "channel": asdict(ctx.channel)}
        if not section:
            return all_
        if section not in all_:
            raise OpsError(f"没有设置分区「{section}」，可用: {', '.join(all_)}")
        return {section: all_[section]}

    def settings_set(self, section: str, values: Dict[str, Any]) -> Dict[str, Any]:
        """Change settings in place and save them. diag: {addr:{phys,func,resp,extended}, keepalive:{...}, settings:{...}};
        security / log / channel: flat fields (see settings_get)."""
        ctx = self.ctx
        if not isinstance(values, dict) or not values:
            raise OpsError("values 应为非空对象")
        if section == "diag":
            cur = copy.deepcopy(ctx.diag_cfg)
            for part, vals in values.items():
                if part == "addr":
                    try:
                        merged = {**cur.addr.to_dict(), **vals}
                        new = Addressing.from_dict(merged)
                    except (ValueError, TypeError, AttributeError) as e:
                        raise OpsError(f"地址设置无效: {e}")
                    cur.addr = new
                elif part in ("keepalive", "settings"):
                    if not isinstance(vals, dict):
                        raise OpsError(f"diag.{part} 应为对象")
                    _typed_merge(getattr(cur, part), vals, f"diag.{part}")
                else:
                    raise OpsError(f"diag 没有分区「{part}」，可用: addr, keepalive, settings")
            self._check_diag(cur)
            self._assign(ctx.diag_cfg.addr, cur.addr)
            self._assign(ctx.diag_cfg.keepalive, cur.keepalive)
            self._assign(ctx.diag_cfg.settings, cur.settings)
        elif section == "security":
            cur = copy.deepcopy(ctx.security)
            vals = dict(values)
            idm = vals.pop("id_map", None)
            _typed_merge(cur, vals, "security")
            if idm is not None:
                try:
                    cur.id_map = {str(k).lower(): [int(x) for x in v] for k, v in idm.items()}
                    cur.id_map_int()
                except (ValueError, TypeError, AttributeError, IndexError):
                    raise OpsError('security.id_map 应形如 {"7e2": [0, 0]}（CAN ID 十六进制 → 两个整数）')
            if cur.serial_format not in ("ascii", "hex"):
                raise OpsError("security.serial_format 应为 ascii 或 hex")
            self._assign(ctx.security, cur)
        elif section == "log":
            cur = copy.deepcopy(ctx.logcfg)
            _typed_merge(cur, values, "log")
            try:
                cur.validate()
            except ValueError as e:
                raise OpsError(str(e))
            self._assign(ctx.logcfg, cur)
        elif section == "channel":
            cur = copy.deepcopy(ctx.channel)
            _typed_merge(cur, values, "channel")
            if cur.sim_profile not in ("old", "new"):
                raise OpsError('channel.sim_profile 应为 "old" 或 "new"')
            self._assign(ctx.channel, cur)
        else:
            raise OpsError(f"没有设置分区「{section}」，可用: diag, security, log, channel")
        ctx.save()
        return {"message": "已保存", **self.settings_get(section)}

    @staticmethod
    def _assign(dst, src) -> None:
        for f in fields(dst):
            setattr(dst, f.name, getattr(src, f.name))

    @staticmethod
    def _check_diag(c: DiagConfig) -> None:
        s = c.settings
        for name, lo, hi in (("p2_ms", 1, 60000), ("p2star_ms", 1, 600000), ("fc_timeout_ms", 1, 60000), ("bs", 0, 255),
                             ("st_min_ms", 0, 127), ("padding", 0, 255), ("rx_padding", 0, 255), ("override_stmin_ms", 0, 127)):
            v = getattr(s, name)
            if not lo <= v <= hi:
                raise OpsError(f"diag.settings.{name} 应在 {lo}~{hi} 之间")
        if c.keepalive.addr not in ("func", "phys"):
            raise OpsError('diag.keepalive.addr 应为 "func" 或 "phys"')
        if not 20 <= c.keepalive.period_ms <= 60000:
            raise OpsError("diag.keepalive.period_ms 应在 20~60000 之间")
        for nm in ("phys", "func", "resp"):
            limit = 0x1FFFFFFF if c.addr.extended else 0x7FF
            if not 0 <= getattr(c.addr, nm) <= limit:
                raise OpsError(f"diag.addr.{nm} 超出{'扩展' if c.addr.extended else '标准'}帧 ID 范围")

    # ------------------------------------------------------------------ CAN file transfer (zdiag/cft.py)
    def _ft_open(self, ch: int):
        """(bus, fd, brs, release) of a started channel for the file transfer (tests replace this)."""
        bus, _ = self._bus(ch)
        cfg = self.ctx.dev.cfg_of(ch)
        return bus, bool(cfg and cfg.is_fd), bool(cfg and cfg.brs), lambda: self._drop(ch)

    def _ft_valid(self, ch: int, bus) -> bool:
        """Is `bus` still the live bus of channel `ch` (device open, channel started)?  (tests replace this)"""
        d = self.ctx.dev
        c = d.chans.get(ch)
        return bool(d.is_open and c is not None and c.bus is bus)

    def _ft_config(self, base_id: str, chunk_bytes: int, window: int, fd_frame_len: int, extended: bool, overwrite: bool = True,
                   link_timeout_ms: int = 600, idle_timeout_s: int = 30, persist: bool = True):
        try:
            c = cftmod.CftConfig(persist=bool(persist), base_id=parse_id(base_id) if isinstance(base_id, str) else int(base_id), extended=bool(extended),
                                 chunk_bytes=_int(chunk_bytes, "块大小", 64, 15000), window=_int(window, "窗口", 1, 128),
                                 fd_frame_len=_int(fd_frame_len, "FD 帧长度", 12, 64), overwrite=bool(overwrite),
                                 link_timeout=_int(link_timeout_ms, "链路超时(ms)", 100, 10000) / 1000.0,
                                 idle_timeout=float(_int(idle_timeout_s, "无进展超时(s)", 2, 86400)))
            c.validate()
        except (ValueError, cftmod.CftError) as e:
            raise OpsError(str(e))
        return c

    def _ft_start(self, role: str, channels: List[int], cfg, make) -> Dict[str, Any]:
        chans = [int(c) for c in (channels if isinstance(channels, (list, tuple)) else [channels])]
        if not chans or len(set(chans)) != len(chans):
            raise OpsError("channels 需要 1~2 个不重复的通道号，例如 [0, 1]")
        with self.lock:
            if role in self._ft and self._ft[role].running():
                raise OpsError("已有这个方向的文件传输在进行，请先 ft_stop")
            busy = {c for j in self._ft.values() if j.running() for c in j.channels}
        if busy & set(chans):
            raise OpsError(f"通道 {sorted(busy & set(chans))} 正被另一个文件传输使用")
        rels: Dict[int, Any] = {}                                  # channel -> release callback while the link holds the channel
        rlock = threading.Lock()
        links: List[Any] = []

        def let_go(ch: int) -> None:
            with rlock:
                rel = rels.pop(ch, None)
            if rel is not None:
                rel()

        def mk_provider(ch: int):
            def provider():
                try:
                    bus, fd, brs, rel = self._ft_open(ch)
                except Exception:
                    return None                                    # not started yet: the supervisor tries again
                with rlock:
                    rels[ch] = rel
                return bus, fd, brs
            return provider

        by_ch: Dict[int, Any] = {}

        def new_link(ch: int, idx: int):
            l = cftmod.Link(idx, None, False, False, cfg, f"通道{ch}")
            l.provider = mk_provider(ch)
            l.validator = (lambda b, ch=ch: self._ft_valid(ch, b))
            l.on_attach = (lambda lk, up, ch=ch: None if up else let_go(ch))
            got = l.provider()
            if got:
                l.attach(*got)
            elif not cfg.persist:
                raise OpsError(f"通道{ch} 还没有打开：请先打开设备并启动该通道")
            by_ch[ch] = l
            return l

        try:
            for i, ch in enumerate(chans):
                links.append(new_link(ch, i))
            engine = make(links)
        except Exception as e:
            for ch in chans:
                let_go(ch)
            raise OpsError(str(e))

        def done() -> None:
            for l in links:
                l.detach("传输结束")
            for ch in chans:
                let_go(ch)
        job = cftmod.CftJob(engine, engine._runner, done)
        job.channels = chans

        def retarget(new: List[int]) -> Dict[str, Any]:
            """Change the channel set of the RUNNING job: new ones join at once (waiting until open), removed ones leave."""
            with self.lock:
                busy = {c for r, j in self._ft.items() if j is not job and j.running() for c in j.channels}
            if busy & set(new):
                raise OpsError(f"通道 {sorted(busy & set(new))} 正被另一个文件传输使用")
            for ch in [c for c in job.channels if c not in new]:
                engine.remove_link(by_ch.pop(ch))
            for ch in [c for c in new if c not in job.channels]:
                engine.add_link(new_link(ch, len(engine.links)))
            job.channels[:] = list(new)
            return {"channels": list(job.channels), "waiting_channels": [c for c in job.channels if not by_ch[c].attached]}
        job.retarget = retarget
        with self.lock:
            self._ft[role] = job
        job.start()
        waiting = [c for c, l in zip(chans, links) if not l.attached]
        return {"started": True, "role": role, "channels": chans, "waiting_channels": waiting,
                "message": "已开始，用 ft_status 查看进度" + (f"；通道 {waiting} 还没打开，打开后会自动连接" if waiting else "")}

    def ft_channels(self, role: str, channels: List[int]) -> Dict[str, Any]:
        """Change the channels of a running file transfer (role 'send' / 'receive') without stopping it: newly listed channels join
        immediately (waiting until they are open), channels left out are released.  Do the same on both ends to use more channels."""
        new = [int(c) for c in (channels if isinstance(channels, (list, tuple)) else [channels])]
        if not new or len(set(new)) != len(new) or len(new) > 2:
            raise OpsError("channels 需要 1~2 个不重复的通道号")
        with self.lock:
            job = self._ft.get(role)
        if job is None or not job.running():
            raise OpsError(f"没有正在进行的 {role} 传输")
        return job.retarget(new)

    def ft_send(self, channels: List[int], paths: List[str], base_id: str = "6A0", chunk_bytes: int = 1024, window: int = 128,
                fd_frame_len: int = 64, extended: bool = False, link_timeout_ms: int = 600, idle_timeout_s: int = 30,
                reconnect: bool = True) -> Dict[str, Any]:
        """Send files / whole folders to a peer running ft_receive, over 1~2 CAN / CAN FD channels (background job).
        With two channels the file is split between them; if one goes silent the other carries on and the lost one is used again when it
        returns. reconnect=True (default): from the start it keeps watching the chosen channels - not-yet-open channels are attached as soon
        as they are open, a channel whose device was unplugged / closed is re-attached when it is started again, and the transfer resumes by
        itself (it never gives up; ft_stop ends it). A transfer that was interrupted continues where it stopped when the same files are sent again."""
        ps = [paths] if isinstance(paths, str) else list(paths or [])
        if not ps:
            raise OpsError("需要要发送的文件或文件夹路径 (paths)")
        for p in ps:
            if not os.path.exists(p):
                raise OpsError(f"路径不存在: {p}")
        cfg = self._ft_config(base_id, chunk_bytes, window, fd_frame_len, extended, True, link_timeout_ms, idle_timeout_s, reconnect)

        def make(links):
            e = cftmod.Sender(links, cfg)
            e._runner = lambda: e.run(ps)
            return e
        return self._ft_start("send", channels, cfg, make)

    def ft_receive(self, channels: List[int], dest: str, base_id: str = "6A0", overwrite: bool = True, once: bool = True,
                   chunk_bytes: int = 1024, window: int = 128, fd_frame_len: int = 64, extended: bool = False,
                   reconnect: bool = True) -> Dict[str, Any]:
        """Receive into the folder `dest` (created when missing); base_id / extended / fd_frame_len must match the sender.
        once=True ends after one sender finished, False keeps listening until ft_stop. Partial files stay as <name>.cftpart for resuming."""
        if not dest:
            raise OpsError("需要保存目录 (dest)")
        cfg = self._ft_config(base_id, chunk_bytes, window, fd_frame_len, extended, overwrite, persist=reconnect)

        def make(links):
            e = cftmod.Receiver(links, dest, cfg, once=bool(once))
            e._runner = e.run
            return e
        return self._ft_start("receive", channels, cfg, make)

    def ft_status(self) -> Dict[str, Any]:
        out = {r: j.status() for r, j in self._ft.items()}
        return {"jobs": out, "active": [r for r, j in self._ft.items() if j.running()]} if out else {"jobs": {}, "active": [],
                                                                                                          "message": "还没有文件传输"}

    def ft_stop(self, role: str = "", wait: float = 10.0) -> Dict[str, Any]:
        """Stop the transfer(s). The stop flag is set first and always takes effect at once; `wait` only bounds how long we wait
        for the worker threads to wind down (0 = do not wait; the GUI uses that)."""
        jobs = [j for r, j in self._ft.items() if j.running() and role in ("", r)]
        for j in jobs:
            j.engine.stop()
        for j in jobs:
            if wait > 0:
                j.thread.join(timeout=wait)
        return {"message": f"已停止 {len(jobs)} 个文件传输" if jobs else "没有正在进行的文件传输", **self.ft_status()}

    # ------------------------------------------------------------------ LIN (UDS over LIN)
    def _lin_master(self):
        if self._lin is None:
            raise OpsError("LIN 通道还没有打开：先调用 lin_open")
        return self._lin["master"]

    def lin_open(self, kind: str = "sim", channel: int = 0, baud: int = 19200, device: str = "", dll: str = "") -> Dict[str, Any]:
        """Open the LIN channel. kind = "sim" (simulated oil pump on NAD 21) | "zlg" (ZLG box LIN channel; needs the SDK LIN
        definitions, not wired yet). baud: LIN bit rate, 19200 by default (valid 1000..20000)."""
        if kind not in ("sim", "zlg"):
            raise OpsError('kind 应为 "sim" 或 "zlg"')
        baud = _int(baud, "LIN 波特率", 1000, 20000)
        if self._lin is not None:
            return {"message": "LIN 通道已打开", **self.lin_status()}
        if kind == "sim":
            master = linmod.SimLinMaster()
        else:
            from . import zlglin
            try:
                master = zlglin.open_zlg(dll, device, _int(channel, "通道", 0, 15), baud)
            except linmod.LinError as e:
                raise OpsError(str(e))
        self._lin = {"master": master, "kind": kind, "channel": int(channel), "baud": baud}
        return {"message": "LIN 通道已打开", **self.lin_status()}

    def lin_close(self) -> Dict[str, Any]:
        st, self._lin = self._lin, None
        if st is not None:
            try:
                st["master"].close()
            except Exception:
                pass
        return {"message": "LIN 通道已关闭" if st else "LIN 通道本来就没打开", **self.lin_status()}

    def lin_status(self) -> Dict[str, Any]:
        st = self._lin
        return {"lin_open": st is not None, **({"kind": st["kind"], "channel": st["channel"], "baud": st["baud"]} if st else {})}

    @staticmethod
    def _nad(v: Any) -> int:
        try:
            n = int(str(v), 16) if isinstance(v, str) else int(v)
        except ValueError:
            raise OpsError(f"NAD 不是有效的十六进制数: {v!r}")
        if not 0 <= n <= 0xFF:
            raise OpsError("NAD 应在 00..FF")
        return n

    def lin_request(self, nad: str, pdu: str, parse: str = "hex", p2_ms: int = 300) -> Dict[str, Any]:
        """One UDS request to the node `nad` (hex) over LIN (master request 3C / slave response 3D)."""
        master = self._lin_master()
        req = _hex(pdu, "请求报文")
        if not req:
            raise OpsError("请求报文为空")
        if parse not in ("hex", "ascii", "dec"):
            raise OpsError("parse 应为 hex / ascii / dec")
        n = self._nad(nad)
        r = linmod.request(master, n, req, _int(p2_ms, "p2_ms", 10, 10000) / 1000.0)
        out: Dict[str, Any] = {"ok": r["ok"], "nad": f"{n:02X}", "request": fmt_hex(req)}
        if r["ok"]:
            data = bytes(r["data"])
            s = default_skip(req)
            out.update(response=fmt_hex(data), data=fmt_hex(data), value=render_result(data[s:], parse))
        else:
            out["error"] = r.get("error", "无响应")
            if r.get("nrc") is not None:
                out.update(response=fmt_hex(r["data"]), nrc=f"{r['nrc']:02X}", nrc_text=NRC_TEXT.get(r["nrc"], ""))
        return out

    def lin_scan_nodes(self, first: str = "01", last: str = "7D", p2_ms: int = 50) -> Dict[str, Any]:
        """Probe every NAD in [first, last] with TesterPresent; lists the nodes that answered."""
        master = self._lin_master()
        a, b = self._nad(first), self._nad(last)
        if a > b:
            raise OpsError("起始 NAD 不能大于结束 NAD")
        found = linmod.scan_nodes(master, a, b, _int(p2_ms, "p2_ms", 10, 2000) / 1000.0)
        return {"nodes": [f"{n:02X}" for n in found], "count": len(found), "range": f"{a:02X}-{b:02X}"}

    def lin_scan_pump(self, nad: str, dids: Optional[List[Any]] = None, session: bool = True, p2_ms: int = 300) -> Dict[str, Any]:
        """Read the oil pump's information. Default DIDs: standard identification set (F187 零件号, F189/F195 软件版本,
        F18A 供应商, F18C 序列号, F191/F193 硬件版本). `dids` = list of "F190" or {"did":"F190","label":"..","parse":"ascii|hex|dec|auto"}
        for supplier-specific data (speed, temperature, ...)."""
        master = self._lin_master()
        items = None
        if dids:
            items = []
            for d in dids:
                if isinstance(d, dict):
                    did, label, parse = d.get("did", ""), d.get("label", ""), d.get("parse", "auto")
                else:
                    did, label, parse = d, "", "auto"
                b = _hex(did, "DID")
                if len(b) != 2:
                    raise OpsError(f"DID 应为 2 字节: {did!r}")
                if parse not in ("auto", "ascii", "hex", "dec"):
                    raise OpsError("parse 应为 auto / ascii / hex / dec")
                items.append((int.from_bytes(b, "big"), label or fmt_hex(b), parse))
        return linmod.scan_pump(master, self._nad(nad), items, bool(session), _int(p2_ms, "p2_ms", 10, 10000) / 1000.0)

    # ------------------------------------------------------------------ lifecycle
    def shutdown(self) -> None:
        self.function_stop(wait=3.0)
        self._stop_all_ours()


# methods reachable over RPC / MCP (whitelist)
METHODS = ["status", "device_list", "device_open", "device_close", "channel_start", "channel_stop",
           "uds_request", "did_read", "did_write", "dtc_read", "dtc_clear", "ecu_reset", "session_control", "security_unlock",
           "keepalive_start", "keepalive_stop", "modules", "scan", "function_run", "function_start", "function_status", "function_stop",
           "frame_send", "cyclic_start", "cyclic_stop", "cyclic_list",
           "monitor_trace", "monitor_snapshot", "monitor_capture", "monitor_clear", "record_start", "record_stop",
           "settings_get", "settings_set",
           "lin_open", "lin_close", "lin_status", "lin_request", "lin_scan_nodes", "lin_scan_pump",
           "ft_send", "ft_receive", "ft_status", "ft_stop"]
