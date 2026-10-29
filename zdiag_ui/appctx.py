"""Application context: paths, persisted settings, shared CAN connection (no Qt)."""
import json
import os
import sys
from dataclasses import asdict
from typing import Optional

from zdiag.busstats import ChannelCfg
from zdiag.devmgr import ConnError, DeviceManager
from zdiag.project import Channel, DiagConfig, SIM_DEVICE, _fill
from . import services_def


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AppContext:
    def __init__(self, base: Optional[str] = None) -> None:
        self.dir = base or app_dir()
        self.cfg_path = os.path.join(self.dir, "myzcanpro_config.json")
        self.services_path = os.path.join(self.dir, "services.json")
        self.diag_cfg = DiagConfig()
        self.channel = Channel()
        # device manager panel state, persisted: selected device/index + last config per channel number
        self.devsel = {"device": SIM_DEVICE, "index": 0}
        self.chan_cfgs = {}
        self.last_dir = self.dir
        self.services = services_def.load_services(self.services_path)
        self.dev = DeviceManager(self.dir, lambda: self.channel.zlgcan_dll)
        self.load()

    def load(self) -> None:
        try:
            with open(self.cfg_path, "r", encoding="utf-8") as f:
                d = json.load(f)
        except (OSError, ValueError):
            return
        self.diag_cfg = DiagConfig.from_dict(d.get("diag", {}))
        _fill(self.channel, d.get("channel", {}))
        dv = d.get("devsel", {})
        if isinstance(dv.get("device"), str):
            self.devsel["device"] = dv["device"]
        if isinstance(dv.get("index"), int):
            self.devsel["index"] = dv["index"]
        for k, v in (d.get("chan_cfgs") or {}).items():
            try:
                self.chan_cfgs[int(k)] = _fill(ChannelCfg(), v)
            except ValueError:
                pass
        self.last_dir = d.get("last_dir", self.last_dir)

    def save(self) -> None:
        d = {"diag": self.diag_cfg.to_dict(), "channel": asdict(self.channel), "last_dir": self.last_dir,
             "devsel": self.devsel, "chan_cfgs": {str(k): asdict(v) for k, v in self.chan_cfgs.items()}}
        try:
            with open(self.cfg_path, "w", encoding="utf-8") as f:
                json.dump(d, f, ensure_ascii=False, indent=2)
        except OSError:
            pass                                              # read-only location: just don't persist

    def open_session(self, cfg, ch, owner, hooks, base_dir=None):
        """Acquire the shared bus and wrap it in a DiagSession (raises ConnError)."""
        from zdiag.engine import DiagSession
        bus = self.dev.acquire(ch.channel, owner, cfg.addr, ch.sim_profile, cfg.settings.rx_padding)
        ccfg = self.dev.cfg_of(ch.channel)
        return DiagSession(bus, cfg, hooks, base_dir=base_dir or self.dir,
                           can_fd=bool(ccfg and ccfg.is_fd), fd_dl=ch.fd_dl)

    def reload_services(self) -> None:
        self.services = services_def.load_services(self.services_path)

    def save_services(self) -> None:
        try:
            services_def.save_services(self.services_path, self.services)
        except OSError:
            pass
