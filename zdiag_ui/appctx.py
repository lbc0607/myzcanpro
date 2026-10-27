"""Application context: paths, persisted settings, shared CAN connection (no Qt)."""
import json
import os
import sys
import threading
from dataclasses import asdict
from typing import Optional

from zdiag.project import Channel, DiagConfig, SIM_DEVICE, _fill
from zdiag.sim_ecu import make_sim
from . import services_def


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ConnError(Exception):
    pass


class ConnectionManager:
    """One physical device/bus shared by all windows. Windows call begin()/end() around runs;
    a second window cannot start while the first one is mid-run."""

    def __init__(self, ctx: "AppContext") -> None:
        self.ctx = ctx
        self.bus = None
        self.ecu = None
        self.key = None
        self.owner = None
        self._lock = threading.Lock()

    def _key(self, ch: Channel, cfg: DiagConfig):
        a = cfg.addr
        return (ch.device, ch.index, ch.channel, ch.can_type, ch.bitrate, ch.data_bitrate,
                ch.terminator, ch.fd_dl, ch.zlgcan_dll,
                (ch.sim_profile, a.phys, a.resp) if ch.device == SIM_DEVICE else None)

    def acquire(self, ch: Channel, cfg: DiagConfig, owner) -> object:
        with self._lock:
            if self.owner is not None and self.owner is not owner:
                raise ConnError("另一个窗口正在使用设备，请先停止它")
            key = self._key(ch, cfg)
            if self.bus is not None and key == self.key:
                self.owner = owner
                return self.bus
            self._close_locked()
            try:
                if ch.device == SIM_DEVICE:
                    self.bus, self.ecu = make_sim(cfg.addr, ch.sim_profile)
                else:
                    self.bus = self._open_zlg(ch)
            except ConnError:
                raise
            except Exception as e:
                raise ConnError(f"打开设备失败: {e}")
            self.key, self.owner = key, owner
            return self.bus

    def _open_zlg(self, ch: Channel):
        from zdiag.zlgcan_bus import ZlgBus
        dll = ch.zlgcan_dll or os.path.join(self.ctx.dir, "zlgcan.dll")
        if not os.path.isfile(dll):
            raise ConnError(f"找不到 {dll}\n请把 zlgcan.dll 和 kerneldlls 文件夹复制到程序目录，"
                            f"或在「更多设置 → 通道设置」里指定路径。")
        return ZlgBus(dll, ch.device, ch.index, ch.channel, ch.bitrate, ch.data_bitrate,
                      fd=(ch.can_type == "CAN FD"), terminator=ch.terminator)

    def release(self, owner) -> None:
        with self._lock:
            if self.owner is owner:
                self.owner = None

    def _close_locked(self) -> None:
        if self.ecu is not None:
            self.ecu.stop()
        if self.bus is not None:
            try:
                self.bus.close()
            except Exception:
                pass
        self.bus = self.ecu = self.key = None

    def close(self) -> None:
        with self._lock:
            self._close_locked()
            self.owner = None


class AppContext:
    def __init__(self, base: Optional[str] = None) -> None:
        self.dir = base or app_dir()
        self.cfg_path = os.path.join(self.dir, "myzcanpro_config.json")
        self.services_path = os.path.join(self.dir, "services.json")
        self.diag_cfg = DiagConfig()
        self.channel = Channel(device=SIM_DEVICE)
        self.last_dir = self.dir
        self.services = services_def.load_services(self.services_path)
        self.conn = ConnectionManager(self)
        self.load()

    def load(self) -> None:
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.diag_cfg = DiagConfig.from_dict(d.get("diag", {}))
        _fill(self.channel, d.get("channel", {}))
        self.last_dir = d.get("last_dir", self.last_dir)

    def save(self) -> None:
        d = {"diag": self.diag_cfg.to_dict(), "channel": asdict(self.channel), "last_dir": self.last_dir}
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except OSError:
            pass                                              # read-only location: just don't persist

    def open_session(self, cfg, ch, owner, hooks, base_dir=None):
        """Acquire the shared bus and wrap it in a DiagSession (raises ConnError)."""
        from zdiag.engine import DiagSession
        bus = self.conn.acquire(ch, cfg, owner)
        return DiagSession(bus, cfg, hooks, base_dir=base_dir or self.dir,
                           can_fd=(ch.can_type == "CAN FD"), fd_dl=ch.fd_dl)

    def reload_services(self) -> None:
        self.services = services_def.load_services(self.services_path)

    def save_services(self) -> None:
        try:
            services_def.save_services(self.services_path, self.services)
        except OSError:
            pass
