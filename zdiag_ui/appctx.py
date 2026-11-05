"""Application context: paths, persisted settings, shared CAN connection (no Qt)."""
import json
import os
import sys
from dataclasses import asdict
from typing import Optional

from zdiag.busstats import ChannelCfg
from zdiag.logfiles import purge_old
from zdiag.devmgr import ConnError, DeviceManager
from zdiag.project import Channel, DiagConfig, LogCfg, SIM_DEVICE, SecurityCfg, _fill
from zdiag.seedkey import SERIAL_STATE
from . import services_def


def app_dir() -> str:
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class AppContext:
    def __init__(self, base: Optional[str] = None) -> None:
        self.dir = base or app_dir()
        self.cfg_path = os.path.join(self.dir, "CanVDS_config.json")
        self.old_cfg_path = os.path.join(self.dir, "myzcanpro_config.json")      # before the rename: still read once
        self.services_path = os.path.join(self.dir, "services.json")
        self.diag_cfg = DiagConfig()
        self.logcfg = LogCfg()                                     # log folder / file size / retention, shared
        self.security = SecurityCfg()                              # shared by every window, persisted
        self.channel = Channel()
        # device manager panel state, persisted: selected device/index + last config per channel number
        self.devsel = {"device": SIM_DEVICE, "index": 0}
        self.chan_cfgs = {}
        self.last_dir = self.dir
        self.scan_dir = os.path.join(self.dir, "scan_data")      # 软件扫描: modules + executors (folder tree)
        self.services = services_def.load_services(self.services_path)
        self.dev = DeviceManager(self.dir, lambda: self.channel.zlgcan_dll)
        self.load()
        purge_old(self.logcfg.folder(self.dir), self.logcfg.keep_days)           # old automatic logs
        SERIAL_STATE.reset(self.security.folder(self.dir), self.security.serial_file)      # new run: re-read the serial before the 1st unlock

    def load(self) -> None:
        d = None
        for path in (self.cfg_path, self.old_cfg_path):
            try:
                with open(path, "r", encoding="utf-8") as f:
                    d = json.load(f)
                break
            except (OSError, ValueError):
                continue
        if not isinstance(d, dict):
            return
        self.diag_cfg = DiagConfig.from_dict(d.get("diag", {}))
        if isinstance(d.get("logcfg"), dict):
            _fill(self.logcfg, d["logcfg"])
            try:
                self.logcfg.validate()
            except (ValueError, TypeError):
                self.logcfg = LogCfg()
        if isinstance(d.get("security"), dict):
            self.security = SecurityCfg.from_dict(d["security"])
        else:                                                  # configs of earlier versions kept it inside "diag"
            old = d.get("diag", {}) or {}
            legacy = dict((old.get("settings") or {}))
            legacy = {k: v for k, v in legacy.items() if k in ("serial_did", "serial_file", "serial_format")}
            if isinstance(old.get("serial_id_map"), dict):
                legacy["id_map"] = old["serial_id_map"]
            self.security = SecurityCfg.from_dict(legacy)
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
        if isinstance(d.get("scan_dir"), str) and d["scan_dir"]:
            self.scan_dir = d["scan_dir"]

    def save(self) -> None:
        d = {"diag": self.diag_cfg.to_dict(), "security": self.security.to_dict(), "logcfg": asdict(self.logcfg), "channel": asdict(self.channel), "last_dir": self.last_dir, "scan_dir": self.scan_dir,
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
                           can_fd=bool(ccfg and ccfg.is_fd), fd_dl=ch.fd_dl, security=self.security, app_dir=self.dir)

    def reload_services(self) -> None:
        self.services = services_def.load_services(self.services_path)

    def save_services(self) -> None:
        try:
            services_def.save_services(self.services_path, self.services)
        except OSError:
            pass
