"""流程编排: the runner.  Lanes (one per channel) run in parallel threads after the sequential 准备 part.

The runner only talks to an *environment* object (CtxEnv for the real program), so it can be tested without a GUI.
"""
import copy
import dataclasses
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable, Dict, List, Optional

from .busstats import ChannelCfg
from .engine import Hooks
from .flow import (FieldRes, Flow, FlowError, NodeResult, children_of, eval_cond, validate, walk, MAX_LOOP)
from .project import Project, parse_hex, parse_id
from .scanlib import (Executor, INFO_FIELDS, ScanError, ScanStore, cfg_for, new_step, run_executor)
from .sendlist import FD_LENGTHS, SendItem, run_send


@dataclass
class FlowEvent:
    node: str                      # node id
    state: str                     # running | ok | fail | skip | info | reset
    text: str = ""                 # result / reason shown in the list
    lane: int = -1


@dataclass
class FlowReport:
    ok: bool
    results: Dict[str, NodeResult]
    message: str = ""


class _Abort(Exception):
    """A node failed and its on_fail is 'stop': the rest of the lane is not executed."""


class _AnyEvent:
    def __init__(self, *evs) -> None:
        self.evs = evs

    def is_set(self) -> bool:
        return any(e.is_set() for e in self.evs)


# --------------------------------------------------------------------------- environment of the real program
class CtxEnv:
    """Adapter over AppContext (duck typed): store, device manager, sessions."""

    def __init__(self, ctx) -> None:
        self.ctx = ctx
        self.store = ScanStore(ctx.scan_dir)
        self.store.ensure_defaults()

    @property
    def dev(self):
        return self.ctx.dev

    def session(self, channel: int, module, owner, hooks: Optional[Hooks] = None):
        cfg = cfg_for(self.ctx.diag_cfg, module)
        ch = dataclasses.replace(self.ctx.channel, channel=channel)
        return self.ctx.open_session(cfg, ch, owner, hooks or Hooks())

    def bus(self, channel: int, owner):
        return self.ctx.dev.acquire(channel, owner)

    def release(self, owner) -> None:
        self.ctx.dev.release(owner)

    def open_device(self, name: str, index: int) -> str:
        d = self.ctx.dev
        if d.is_open:
            if d.name != name:
                raise FlowError(f"已打开的是 {d.name}，与流程里的 {name} 不一致，请先在设备管理里关闭")
            return "设备已打开"
        d.open(name, index)
        return "已打开"

    def start_channel(self, n: int, cfg: ChannelCfg) -> str:
        d = self.ctx.dev
        if n in d.started():
            return "通道已启动（沿用当前配置）"
        d.start_channel(n, cfg, self.ctx.diag_cfg.addr, self.ctx.channel.sim_profile, self.ctx.diag_cfg.settings.rx_padding)
        return "已启动"

    def channel_cfg(self, n: int) -> Optional[ChannelCfg]:
        return self.ctx.dev.cfg_of(n)


# --------------------------------------------------------------------------- runner
class FlowRunner:
    def __init__(self, flow: Flow, env, cancel: Optional[threading.Event] = None,
                 on_event: Optional[Callable[[FlowEvent], None]] = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self.flow, self.env = flow, env
        self.cancel = cancel or threading.Event()
        self.on_event = on_event or (lambda e: None)
        self._sleep = sleep
        self.results: Dict[str, NodeResult] = {}
        self._lock = threading.Lock()
        self._seen: set = set()
        self._senders: Dict[tuple, dict] = {}                # (lane, key) -> {"thread","ev","node","res"}

    # -- helpers
    def _emit(self, node: str, state: str, text: str = "", lane: int = -1) -> None:
        with self._lock:
            self._seen.add(node)
        try:
            self.on_event(FlowEvent(node, state, text, lane))
        except Exception:
            pass

    def _wait(self, seconds: float) -> None:
        end = time.perf_counter() + seconds
        while not self.cancel.is_set():
            left = end - time.perf_counter()
            if left <= 0:
                return
            self._sleep(min(left, 0.05))

    def _set(self, node: dict, res: NodeResult) -> NodeResult:
        with self._lock:
            self.results[node["id"]] = res
        return res

    def _skip_subtree(self, nodes: List[dict], why: str, lane: int) -> None:
        for n in walk(nodes):
            if n["id"] not in self._seen:
                self._set(n, NodeResult(ok=True, skipped=True))
                self._emit(n["id"], "skip", why, lane)

    # -- entry
    def run(self) -> FlowReport:
        errs = validate(self.flow, getattr(self.env, "store", None))
        if errs:
            return FlowReport(False, {}, "流程有问题，未执行:\n" + "\n".join("· " + e for e in errs))
        ok = True
        setup = self.flow.setup()
        if setup is not None and setup.get("enabled", True):
            self._emit(setup["id"], "running")
            try:
                self._run_list(setup["children"], -1, owner=None)
                self._emit(setup["id"], "ok", "准备完成")
            except _Abort:
                self._emit(setup["id"], "fail", "准备失败，流程已停止")
                self._skip_subtree(self.flow.nodes, "未执行（准备失败）", -1)
                return FlowReport(False, self.results, "准备失败")
        lanes = [l for l in self.flow.lanes() if l.get("enabled", True)]
        outcome: Dict[str, bool] = {}
        threads = []
        for lane in lanes:
            t = threading.Thread(target=self._run_lane, args=(lane, outcome), name=f"flow-lane{lane['channel']}", daemon=True)
            threads.append(t)
            t.start()
        for t in threads:
            t.join()
        ok = all(outcome.get(l["id"], False) for l in lanes)
        if self.cancel.is_set():
            return FlowReport(False, self.results, "已停止")
        return FlowReport(ok, self.results, "全部完成" if ok else "有泳道执行失败")

    def _run_lane(self, lane: dict, outcome: Dict[str, bool]) -> None:
        owner = object()
        ch = lane["channel"]
        self._emit(lane["id"], "running", lane=ch)
        ok = True
        try:
            self._run_list(lane["children"], ch, owner)
        except _Abort:
            ok = False
        except Exception as e:                               # never lose a lane silently
            ok = False
            self._emit(lane["id"], "fail", f"内部错误: {e}", ch)
        finally:
            self._stop_senders(ch)
            try:
                self.env.release(owner)
            except Exception:
                pass
        if self.cancel.is_set():
            ok = False
            self._skip_subtree(lane["children"], "已停止", ch)
            self._emit(lane["id"], "fail", "已停止", ch)
        elif ok:
            self._emit(lane["id"], "ok", "完成", ch)
        else:
            self._skip_subtree(lane["children"], "未执行（前面的步骤失败）", ch)
            self._emit(lane["id"], "fail", "失败，后面的步骤未执行", ch)
        outcome[lane["id"]] = ok

    # -- node list / node
    def _run_list(self, nodes: List[dict], lane: int, owner) -> None:
        for n in nodes:
            if self.cancel.is_set():
                raise _Abort()
            if not n.get("enabled", True):
                self._set(n, NodeResult(skipped=True))
                self._emit(n["id"], "skip", "已禁用", lane)
                self._skip_subtree([n], "已禁用", lane)
                continue
            self._exec(n, lane, owner)

    def _exec(self, n: dict, lane: int, owner) -> None:
        t = n["type"]
        self._emit(n["id"], "running", "", lane)
        try:
            if t == "if":
                self._exec_if(n, lane, owner)
                return
            if t == "loop":
                self._exec_loop(n, lane, owner)
                return
            res = self._leaf(n, lane, owner)
        except _Abort:
            raise
        except FlowError as e:
            res = NodeResult(False, error=str(e))
        except Exception as e:
            res = NodeResult(False, error=f"{type(e).__name__}: {e}")
        self._set(n, res)
        if res.ok:
            self._emit(n["id"], "ok", res.text, lane)
        else:
            self._emit(n["id"], "fail", res.error or "失败", lane)
            if n.get("on_fail", "stop") == "stop":
                raise _Abort()

    def _fail(self, n: dict, lane: int, msg: str) -> None:
        self._set(n, NodeResult(False, error=msg))
        self._emit(n["id"], "fail", msg, lane)
        if n.get("on_fail", "stop") == "stop":
            raise _Abort()

    def _exec_if(self, n: dict, lane: int, owner) -> None:
        try:
            truth, why = eval_cond(n["cond"], self.results)
        except FlowError as e:
            self._fail(n, lane, str(e))
            return
        taken, other = (n["then"], n["else"]) if truth else (n["else"], n["then"])
        self._set(n, NodeResult(True, text=("成立 → 执行「成立时」" if truth else "不成立 → 执行「否则」"), ))
        self._emit(n["id"], "ok", f"{why}  →  {'成立' if truth else '不成立'}", lane)
        self._skip_subtree(other, "未走此分支", lane)
        self._run_list(taken, lane, owner)

    def _exec_loop(self, n: dict, lane: int, owner) -> None:
        mode, body = n.get("mode", "count"), n["body"]
        limit = int(n["count"]) if mode == "count" else int(n.get("max_iter", 100))
        done = 0
        while done < min(limit, MAX_LOOP):
            if self.cancel.is_set():
                raise _Abort()
            if mode == "while":
                try:
                    truth, why = eval_cond(n["cond"], self.results)
                except FlowError as e:
                    self._fail(n, lane, str(e))
                    return
                if not truth:
                    break
            for b in walk(body):                              # clear the previous iteration's marks
                self._emit(b["id"], "reset", "", lane)
            self._emit(n["id"], "info", f"第 {done + 1} 次", lane)
            self._run_list(body, lane, owner)
            done += 1
            if mode == "until":
                try:
                    truth, why = eval_cond(n["cond"], self.results)
                except FlowError as e:
                    self._fail(n, lane, str(e))
                    return
                if truth:
                    break
        msg = f"共执行 {done} 次"
        if mode != "count" and done >= limit:
            msg += f"（达到上限 {limit} 次）"
        self._set(n, NodeResult(True, text=msg))
        self._emit(n["id"], "ok", msg, lane)

    # -- leaves
    def _module(self, n: dict):
        for m in self.env.store.modules():
            if m.name == n.get("module"):
                return m
        raise FlowError(f"找不到软件模块「{n.get('module')}」")

    def _leaf(self, n: dict, lane: int, owner) -> NodeResult:
        t = n["type"]
        if t == "note":
            return NodeResult(True, text=n.get("text", ""))
        if t == "delay":
            self._wait(float(n["seconds"]))
            if self.cancel.is_set():
                raise _Abort()
            return NodeResult(True, text=f"已延时 {float(n['seconds']):g} 秒")
        if t == "open_device":
            return NodeResult(True, text=self.env.open_device(n["device"], int(n.get("index", 0))))
        if t == "start_channel":
            return NodeResult(True, text=self.env.start_channel(int(n["channel"]), ChannelCfg(**n["cfg"])))
        if t == "scan":
            return self._scan(n, lane, owner)
        if t in ("function", "uds"):
            return self._run_executor_node(n, lane, owner)
        if t == "send":
            return self._start_send(n, lane, owner)
        if t == "stop_send":
            return self._stop_send_node(n, lane)
        raise FlowError(f"不能执行的节点类型 {t}")

    def _session(self, n: dict, lane: int, owner):
        s = self.env.session(lane, self._module(n), owner)
        s.cancel = self.cancel
        return s

    def _scan(self, n: dict, lane: int, owner) -> NodeResult:
        m = self._module(n)
        sess = self._session(n, lane, owner)
        res = NodeResult(True)
        parts = []
        try:
            for key, label in INFO_FIELDS:
                if key not in n["fields"]:
                    continue
                if self.cancel.is_set():
                    raise _Abort()
                try:
                    out = run_executor(sess, self.env.store.load_info(m, key))
                except ScanError as e:
                    res.fields[key] = FieldRes("-", b"", False)
                    parts.append(f"{label}=-")
                    continue
                ok = out.ok and bool(out.text)
                res.fields[key] = FieldRes(out.text if ok else "-", out.raw if ok else b"", ok)
                parts.append(f"{label}={res.fields[key].text}")
        finally:
            sess.close()
        res.text = "  ".join(parts)
        if not any(f.ok for f in res.fields.values()):
            res.ok, res.error = False, "所有字段都读取失败（无响应或否定响应）"
        return res

    def _run_executor_node(self, n: dict, lane: int, owner) -> NodeResult:
        m = self._module(n)
        if n["type"] == "uds":
            steps = []
            if n.get("pre_session"):
                steps.append(new_step("service", name="进入扩展会话", pdu="10 03"))
            steps.append(new_step("service", name=n.get("name", "请求"), pdu=n["pdu"]))
            ex = Executor(n.get("name", "UDS请求"), Project(name="uds", steps=steps),
                          {"step": -1, "skip": int(n.get("skip", -1)), "parse": n.get("parse", "hex")})
        else:
            path = os.path.join(self.env.store.func_dir(m), *str(n["path"]).replace("\\", "/").split("/")) + ".json"
            ex = Executor.load(path)
        sess = self._session(n, lane, owner)
        try:
            out = run_executor(sess, ex)
        finally:
            sess.close()
        if self.cancel.is_set():
            raise _Abort()
        if not out.ok:
            return NodeResult(False, error=out.error)
        return NodeResult(True, text=out.text if out.text else "完成（无返回数据）", raw=out.raw)

    # -- cyclic sending
    def _start_send(self, n: dict, lane: int, owner) -> NodeResult:
        key = (lane, str(n["key"]))
        if key in self._senders:
            raise FlowError(f"周期发送「{n['key']}」已经在运行")
        data = parse_hex(n["data"])
        fd = bool(n.get("fd"))
        length = len(data) if not fd else next((x for x in FD_LENGTHS if x >= len(data)), 64)
        item = SendItem(can_id=parse_id(str(n["can_id"])), extended=bool(n.get("extended")), length=length, data=data,
                        count=-1, interval_ms=int(n["period_ms"]))
        bus = self.env.bus(lane, owner)
        cfg = self.env.channel_cfg(lane) if hasattr(self.env, "channel_cfg") else None
        if fd and not (cfg and cfg.is_fd):
            raise FlowError("通道不是 CAN FD 模式，不能发送 FD 帧")
        brs = bool(fd and cfg and cfg.fd_accel)
        ev = threading.Event()
        info = {"ev": ev, "node": n["id"], "res": None}

        def go() -> None:
            try:
                info["res"] = run_send(bus, [item], fd=fd, brs=brs, cancel=_AnyEvent(ev, self.cancel), stop_on_fail=True)
            except Exception as e:
                info["err"] = str(e)

        t = threading.Thread(target=go, name="flow-send", daemon=True)
        info["thread"] = t
        self._senders[key] = info
        t.start()
        return NodeResult(True, text=f"已开始，每 {n['period_ms']} ms 发送一次")

    def _sent_text(self, info: dict) -> str:
        r = info.get("res")
        if info.get("err"):
            return f"发送出错: {info['err']}"
        if r is None:
            return "已停止"
        ok, bad = r.per_row.get(0, [0, 0])
        return f"已停止，共发送 {ok} 帧" + (f"，失败 {bad} 帧" if bad else "")

    def _finish_sender(self, key: tuple, lane: int) -> str:
        info = self._senders.pop(key)
        info["ev"].set()
        info["thread"].join(timeout=3)
        text = self._sent_text(info)
        res = self.results.get(info["node"])
        if res is not None:
            res.text = text
        self._emit(info["node"], "ok", text, lane)
        return text

    def _stop_send_node(self, n: dict, lane: int) -> NodeResult:
        k = str(n.get("key", "")).strip()
        keys = [x for x in list(self._senders) if x[0] == lane and (not k or x[1] == k)]
        if not keys:
            raise FlowError("没有正在运行的周期发送" + (f"「{k}」" if k else ""))
        return NodeResult(True, text="；".join(f"{x[1]}: {self._finish_sender(x, lane)}" for x in keys))

    def _stop_senders(self, lane: int) -> None:
        for key in [x for x in list(self._senders) if x[0] == lane]:
            self._finish_sender(key, lane)
