"""Smoke test with the REAL Qt binding (run in CI with QT_QPA_PLATFORM=offscreen; skipped without PyQt5/PySide2).

Builds every window/dialog, drives the main flows against the simulator and fails on any exception."""
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import traceback

for _st in (sys.stdout, sys.stderr):                      # Windows console is cp1252: Chinese labels would raise on print
    try:
        _st.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass


def _hook(et, ev, tb):                                    # any uncaught error -> CI annotation (raw logs may be unreadable)
    t = "".join(traceback.format_exception(et, ev, tb))
    print(t)
    print("::error title=smoke::" + t[-1500:].replace("%", "%25").replace("\r", "").replace("\n", "%0A"))


sys.excepthook = _hook
try:
    from zdiag_ui import qt
except ImportError:
    print("no Qt binding installed - skipped")
    sys.exit(0)
from zdiag_ui.qt import QtWidgets as W

sys.path.insert(0, os.path.dirname(__file__))
import test_engine as E                                   # ihex(), SEG_A/SEG_B (stub-free helpers)
from zdiag.busstats import ChannelCfg
from zdiag.project import SIM_DEVICE, sample_project
from zdiag_ui import common, dialogs
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
    for key in ("send", "monitor", "scan", "crc"):
        L._open(key)
        check(f"window {key} shows", L.windows[key].isVisible())
    dlg = dialogs.SettingsDialog(ctx, ctx.diag_cfg, ctx.channel)
    dlg.show()
    dlg.btn_ok.click()
    check("settings dialog builds and applies", True)
    cd = dialogs.ChannelConfigDialog(SIM_DEVICE, 0, ctx.chan_cfgs.get(0) or ChannelCfg())
    cd.c_proto.setCurrentText("CAN FD")
    cd.show()
    check("channel dialog builds (FD)", cd.c_dbaud.isEnabled())
    cd.close()
    check("tools are tabs", L.tabs.count() == 5)
    L.float_tool("monitor"); L.dock_tool("monitor")        # undock / dock round trip with the real Qt
    check("monitor docked again", "monitor" not in L.floating and L.tabs.indexOf(L.windows["monitor"]) >= 0)
    for key in ("send", "scan"):                           # a channel serves one window at a time: close them again
        L.close_tool(key)
    check("no window keeps the channel", ctx.dev.owners == {})
    # --- 报文监视 stays open (and records) while everything below runs
    mon = L.windows["monitor"]
    rec_path = os.path.join(tempfile.mkdtemp(), "live.csv")
    W.QFileDialog.getSaveFileName = staticmethod(lambda *a, **k: (rec_path, ""))
    mon.b_rec.click()
    check("monitor recording started", ctx.dev.tap.recorder is not None)
    # --- 普通发送
    L._open("send")
    t = L.windows["send"].tabs.widget(0)
    t.e_id.setText("7e2"); t.e_data.setText("02 10 01"); t.b_now.click()
    pump(lambda: not t.running() and ctx.dev.stats_of(0).snapshot()[1] >= 1, 10)
    check("send window transmitted a frame", ctx.dev.stats_of(0).snapshot()[1] >= 1)
    t.b_add.click()
    check("added to list", t.table.rowCount() == 1 and t.table.item(0, 2).text() == "7E2")
    t.b_list.click()
    pump(lambda: not t.running(), 10)
    check("list send finished", not t.running())
    L.close_tool("send")
    check("send window released the channel", pump(lambda: ctx.dev.owners == {}, 5))   # release runs in the GUI thread
    # --- 软件扫描: scan 4 software modules, open a module's functions, run one
    ctx.scan_dir = tempfile.mkdtemp()
    L._open("scan")
    sw = L.windows["scan"]
    sw.store.__init__(ctx.scan_dir); sw.store.ensure_defaults(); sw.reload_modules()
    sw.b_start.click()
    check("scan finishes", pump(lambda: sw.l_status.text() == "扫描完成", 60))
    vals = [[r["fields"][k].text() for k in ("code", "version", "date", "dtc")] for r in sw.rows]
    check("scan filled every field", len(vals) == 4 and all(v != "-" for row in vals for v in row))
    sw.rows[0]["next"].click()
    check("module page lists default functions", [b.text() for b, _ in sw.tile_buttons] == ["故障检测", "清除故障"])
    sw.tile_buttons[0][0].click()
    check("function runs", pump(lambda: sw.mpanel.l_result.text() not in ("", "运行中…"), 30) and "失败" not in sw.mpanel.l_result.text())
    pump(lambda: ctx.dev.owners == {}, 5)
    sw.rows and sw.b_up.click()
    L.close_tool("scan")
    pump(lambda: ctx.dev.owners == {}, 5)
    # --- a flash 功能 (two-segment HEX, erase + segment/whole-file CRC) with the GLOBAL key DLL, next to the monitor
    from zdiag.engine import Hooks
    d = tempfile.mkdtemp()
    hexp, drv = os.path.join(d, "app.hex"), os.path.join(d, "drv.bin")
    with open(hexp, "w") as f_:
        f_.write(E.ihex([E.SEG_A, E.SEG_B]))
    with open(drv, "wb") as f_:
        f_.write(os.urandom(300))
    p = sample_project("new")
    p.cfg.settings.p2_ms = 800
    ctx.security.key_dll = "builtin:xor"                    # one global setting; the unlock step carries no path
    for s in p.steps:
        if s["type"] == "unlock": s["dll"] = ""
        if s["type"] == "driver": s["file"] = drv
        if s["type"] == "download": s["file"] = hexp
    tok = object()
    sess = ctx.open_session(p.cfg, ctx.channel, tok, Hooks())
    try:
        flash_ok = sess.run_plan(p.steps)
    finally:
        sess.close()
        ctx.dev.release(tok)
    check("flash plan completes", flash_ok)
    ecu = ctx.dev.sim_ecu(0)
    check("ECU saw erase + 3 CRC checks + reset",
          ecu.erased == [(0x08000000, 40, b"\x01")] and [c[1] for c in ecu.crc_checked] == [True] * 3 and ecu.reset_count == 1)
    # --- monitor saw everything, in parallel to the flash session
    mon.refresh(force=True)
    tb = mon.table
    rows = [[tb.item(r, c).text() for c in range(tb.columnCount())] for r in range(tb.rowCount())]
    tx = [r for r in rows if r[3] == "7E2" and r[5] == "发送"]
    check("monitor shows tester TX row for 7E2 with many frames", bool(tx) and int(tx[0][8]) > 50)
    check("monitor shows ECU response row", any(r[3] == "7EA" and r[5] == "接收" for r in rows))
    mon.b_rec.click()
    with open(rec_path, encoding="utf-8-sig") as f:
        nlines = len(f.read().splitlines())
    check("recording file has the frames", nlines > 100)
    # --- stats / bus load on the launcher
    L.refresh_stats(); pump(timeout=0.6); L.refresh_stats()
    check("launcher shows counters", L.panels[0].l_tx.text() != "发送: 0")
    # --- agent port inside the GUI: RPC threads change the device, the launcher must follow in the GUI thread
    from zdiag.rpc import rpc_call
    check("agent port starts", L.start_agent() and L.rpc is not None and L.k_agent.isChecked())
    ep = (L.rpc.url, L.rpc.token)
    check("agent sees the shared device", rpc_call(*ep, "status")["device"] == SIM_DEVICE)
    r = rpc_call(*ep, "uds_request", {"channel": 0, "pdu": "22 F1 90", "parse": "ascii"})
    check("agent uds request answered", r["ok"] and bool(r["value"]))
    rpc_call(*ep, "channel_stop", {"channel": 1})
    check("launcher follows an agent's channel_stop", pump(lambda: L.panels[1].l_state.text() == "未启动", 10))
    check("agent activity shown", "channel_stop" in L.l_agent.text() or pump(lambda: "channel_stop" in L.l_agent.text(), 5))
    L.stop_agent()
    check("agent port stops", L.rpc is None and not L.k_agent.isChecked() and not os.path.exists(os.path.join(ctx.dir, "CanVDS_rpc.json")))
    # --- shutdown
    for k in list(L.windows):
        L.close_tool(k)
    for p_ in L.panels:
        if p_.b_toggle.text() == "停止":
            p_.b_toggle.click()
    L.b_device.click()
    check("device closes", not ctx.dev.is_open)
    L.close()
    bad = [b for b in boxes if b[0] in ("warning", "critical")]
    check(f"no warning/error boxes ({bad})", not bad)
    return 1 if failures else 0


def _annot(level, msg):
    """GitHub Actions annotation (readable through the API even when the raw log is not)."""
    print("::%s title=smoke::%s" % (level, msg.replace("%", "%25").replace("\r", "").replace("\n", "%0A")))


if __name__ == "__main__":
    import traceback
    try:
        rc = main()
    except BaseException:
        tb = traceback.format_exc()
        print(tb)
        _annot("error", tb[-1500:])
        rc = 1
    if failures:
        _annot("error", "FAILED checks: " + "; ".join(failures))
    print("SMOKE", "FAILED" if rc else "PASSED", f"(binding: {qt.BINDING})")
    sys.exit(rc)
