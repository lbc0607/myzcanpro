"""Smoke test with the REAL Qt binding (run in CI with QT_QPA_PLATFORM=offscreen; skipped without PyQt5/PySide2).

Builds every window/dialog, drives the main flows against the simulator and fails on any exception."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
try:
    from zdiag_ui import qt
except ImportError:
    print("no Qt binding installed - skipped")
    sys.exit(0)
from zdiag_ui.qt import QtWidgets as W

sys.path.insert(0, os.path.dirname(__file__))
import test_engine as E                                   # ihex(), SEG_A/SEG_B (stub-free helpers)
from zdiag.project import SIM_DEVICE, sample_project
from zdiag_ui import common, dialogs, flash_window
from zdiag_ui.appctx import AppContext
from zdiag_ui.launcher import LauncherWindow
from zdiag_ui.style import QSS

app = W.QApplication([])
app.setStyleSheet(QSS)
boxes = []
for name in ("information", "warning", "critical", "question"):
    setattr(W.QMessageBox, name, staticmethod(lambda *a, _n=name, **k: (boxes.append((_n, a[1:3])), W.QMessageBox.Yes)[1]))
failures = []


def pump(cond=lambda: False, timeout=20.0):
    end = time.time() + timeout
    while time.time() < end:
        app.processEvents()
        if cond():
            return True
        time.sleep(0.01)
    return cond()


def check(label, ok):
    print(("ok   " if ok else "FAIL ") + label)
    if not ok:
        failures.append(label)


def main() -> int:
    ctx = AppContext(tempfile.mkdtemp())
    L = LauncherWindow(ctx)
    L.show()
    L.c_device.setCurrentText(SIM_DEVICE)
    L.b_device.click()
    check("device opens, button reads 关闭", L.b_device.text() == "关闭" and len(L.panels) == 2)
    dialogs.ChannelConfigDialog.exec_ = lambda d: (d._on_ok(), 1)[1]          # no modal loop in CI
    for p in L.panels:
        p.b_toggle.click()
    check("both channels started", ctx.dev.started() == [0, 1])
    # --- every window / dialog can be built and shown
    for key in ("uds", "flash", "send", "crc"):
        L._open(key)
        check(f"window {key} shows", L.windows[key].isVisible())
    dlg = dialogs.SettingsDialog(ctx, ctx.diag_cfg, ctx.channel)
    dlg.show()
    check("settings dialog applies", dlg.apply() is None)
    cd = dialogs.ChannelConfigDialog(SIM_DEVICE, 0, ctx.chan_cfgs[0])
    cd.c_proto.setCurrentText("CAN FD")
    cd.show()
    check("channel dialog builds (FD)", cd.c_dbaud.isEnabled())
    # --- 普通发送
    t = L.windows["send"].tabs.widget(0)
    t.e_id.setText("7e2"); t.e_data.setText("02 10 01"); t.b_now.click()
    pump(lambda: not t.running() and ctx.dev.stats_of(0).snapshot()[1] >= 1)
    check("send window transmitted a frame", ctx.dev.stats_of(0).snapshot()[1] >= 1)
    t.b_add.click()
    check("added to list", t.table.rowCount() == 1 and t.table.item(0, 2).text() == "7E2")
    t.b_list.click()
    pump(lambda: not t.running())
    check("list send finished", t.table.item(0, 1).text() == "完成")
    # --- UDS诊断
    dw = L.windows["uds"]
    dw.e_pdu.setText("22 F1 90"); dw.b_now.click()
    check("uds 22 F190 answered", pump(lambda: any(e.kind == "RSP" and e.data[:3] == bytes([0x62, 0xF1, 0x90]) for e in dw.events)))
    # --- ECU刷新 (two-segment HEX, erase + segment/whole-file CRC)
    fw = L.windows["flash"]
    d = tempfile.mkdtemp()
    hexp, drv = os.path.join(d, "app.hex"), os.path.join(d, "drv.bin")
    open(hexp, "w").write(E.ihex([E.SEG_A, E.SEG_B]))
    open(drv, "wb").write(os.urandom(300))
    p = sample_project("new")
    p.cfg.settings.p2_ms = 800
    for s in p.steps:
        if s["type"] == "unlock": s["dll"] = "builtin:xor"
        if s["type"] == "driver": s["file"] = drv
        if s["type"] == "download": s["file"] = hexp
    fw.project = p
    fw.editor.refresh(); fw.apply_to_executor()
    ctx.diag_cfg.settings.p2_ms = 800
    fw.executor.b_start.click()
    check("flash plan completes", pump(lambda: not fw.executor.running() and fw.executor.bar.value() == 100, 60))
    ecu = ctx.dev.sim_ecu(0)
    check("ECU saw erase + 3 CRC checks + reset",
          ecu.erased == [(0x08000000, 40, b"\x01")] and [c[1] for c in ecu.crc_checked] == [True] * 3 and ecu.reset_count == 1)
    # --- stats / bus load on the launcher
    L.refresh_stats(); pump(timeout=0.6); L.refresh_stats()
    check("launcher shows counters", L.panels[0].l_tx.text() != "发送: 0")
    # --- shutdown
    for w in list(L.windows.values()):
        w.close()
    for p_ in L.panels:
        if p_.b_toggle.text() == "停止":
            p_.b_toggle.click()
    L.b_device.click()
    check("device closes", not ctx.dev.is_open)
    L.close()
    bad = [b for b in boxes if b[0] in ("warning", "critical")]
    check(f"no warning/error boxes ({bad})", not bad)
    return 1 if failures else 0


if __name__ == "__main__":
    rc = main()
    print("SMOKE", "FAILED" if rc else "PASSED", f"(binding: {qt.BINDING})")
    sys.exit(rc)
