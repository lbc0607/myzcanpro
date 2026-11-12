import json, os, sys, tempfile, time, unittest, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import qt_stub
qt_stub.install()
from PyQt5 import QtWidgets as W          # the fake
from zdiag.busstats import ChannelCfg
from zdiag.devmgr import ConnError
from zdiag.project import SIM_DEVICE, Project, new_step, sample_project
from zdiag.crc import PRESETS
from zdiag_ui.appctx import AppContext
from zdiag_ui.launcher import LauncherWindow
from zdiag_ui.send_window import SendWindow
from zdiag_ui.ft_window import FileTransferWindow
from zdiag_ui.monitor_window import MonitorWindow
from zdiag_ui import scan_window as _sw
from zdiag_ui.scan_window import ScanWindow
from zdiag_ui.dialogs import CrcConfigWidget, CrcToolDialog, ServiceEditorDialog, SettingsDialog
from zdiag_ui import qt as _qt

W.QApplication([])


def fresh_ctx():
    d = tempfile.mkdtemp()
    ctx = AppContext(d)
    ctx.diag_cfg.settings.p2_ms = 800
    ctx.dev.open(SIM_DEVICE)                                   # device manager: open + start channel 0
    ctx.dev.start_channel(0, ChannelCfg(), ctx.diag_cfg.addr, ctx.channel.sim_profile)
    return ctx


def msgs(kind=None):
    return [c for c in W.QMessageBox.calls if kind is None or c[0] == kind]


class Base(unittest.TestCase):
    def setUp(self):
        W.QMessageBox.calls.clear()
        W.QMessageBox.next_answer = W.QMessageBox.Yes
        W.QFileDialog.next_path = ""
        self.ctx = fresh_ctx()
        self.addCleanup(self.ctx.dev.shutdown)

    def tree_item(self, win, sid, sub=None):
        for i in range(win.tree.topLevelItemCount()):
            it = win.tree.topLevelItem(i)
            d = it.data(0, 256)
            if d and d[0] == "svc" and d[1] == sid:
                if sub is None:
                    return it
                for j in range(it.childCount()):
                    if it.child(j).data(0, 256)[2] == sub:
                        return it.child(j)
        raise KeyError((sid, sub))


class SendTests(Base):
    def setUp(self):
        super().setUp()
        self.win = SendWindow(self.ctx); self.t = self.win.tabs.widget(0)
        self.addCleanup(self.win.close)

    def fill(self, id_="7e2", data="02 10 01", length="8", per="1", count="1", itv="0", name="", **kw):
        t = self.t
        t.e_id.setText(id_); t.e_data.setText(data); t.c_len.setCurrentText(length); t.e_per.setText(per)
        t.e_count.setText(count); t.e_int.setText(itv); t.e_name.setText(name)
        t.k_idinc.setChecked(kw.get("idinc", False)); t.k_datainc.setChecked(kw.get("datainc", False))
        t.c_type.setCurrentIndex(1 if kw.get("ext") else 0); t.c_fmt.setCurrentIndex(1 if kw.get("remote") else 0)

    def _log(self):
        return self.ctx.dev.chans[0].bus._bus.log

    def test_add_edit_and_columns(self):
        t = self.t
        self.assertEqual(t.c_ch.count(), 1); self.assertEqual(t.c_len.count(), 9)
        self.fill(name="进入扩展会话"); t.b_add.click()
        self.assertEqual(t.table.cell_texts(0)[2:], ["0", "7E2", "CAN", "8", "进入扩展会话", "02 10 01 00 00 00 00 00", "标准数据帧", "1", "1", "0"])
        c = t.table.item(0, 3); c.setText("7df"); self.assertEqual(t.items[0].can_id, 0x7DF)
        W.QMessageBox.calls.clear(); t.table.item(0, 10).setText("0")                          # invalid -> reverted
        self.assertTrue(msgs("warn")); self.assertEqual(t.items[0].count, 1); self.assertEqual(t.table.item(0, 10).text(), "1")
        t.table.item(0, 10).setText("-1"); self.assertEqual(t.items[0].count, -1)               # forever
        W.QMessageBox.calls.clear(); t.table.item(0, 10).setText("9000000000"); self.assertTrue(msgs("warn")); self.assertEqual(t.items[0].count, -1)
        t.table.item(0, 10).setText("9000000"); self.assertEqual(t.items[0].count, 9000000)    # big but legal, no crash
        t.table.item(0, 10).setText("1")
        t.table.item(0, 7).setText("01 02 03"); self.assertEqual(t.items[0].data, bytes([1, 2, 3]))
        self.fill(length="2", data="01 02 03"); W.QMessageBox.calls.clear(); t.b_add.click()
        self.assertTrue(msgs("warn")); self.assertEqual(len(t.items), 1)                      # data longer than length

    def test_send_now_pads_to_length_and_counts(self):
        self.fill(count="3", itv="5", idinc=True, per="2"); self.t.b_now.click()
        frames = [(f.can_id, f.data.hex(" ")) for f in self._log()]
        self.assertEqual(frames, [(0x7E2 + i, "02 10 01 00 00 00 00 00") for i in range(6)])
        self.assertEqual(self.ctx.dev.stats_of(0).snapshot()[1], 6)
        self.assertTrue(self.t.l_time.text().startswith("发送时间(s): 0.")); self.assertEqual(self.ctx.dev.owners, {})

    def test_list_send_checked_rows_modes_and_status(self):
        t = self.t
        self.fill(id_="100", data="AA", count="2", itv="10", length="1"); t.b_add.click()
        self.fill(id_="200", data="BB", count="2", itv="10", length="1"); t.b_add.click()
        self.fill(id_="300", data="CC", length="1"); t.b_add.click()
        t.table.item(2, 0).setCheckState(0)                                                   # untick the 3rd row
        self.assertFalse(t.items[2].checked)
        t.c_mode.setCurrentText("串行发送模式"); t.b_list.click()
        self.assertEqual([f.can_id for f in self._log()], [0x100, 0x100, 0x200, 0x200])
        self.assertEqual([t.table.item(r, 1).text() for r in range(3)], ["完成", "完成", ""])
        self._log().clear()
        t.c_mode.setCurrentText("并行发送模式"); t.b_list.click()
        self.assertEqual([f.can_id for f in self._log()], [0x100, 0x200, 0x100, 0x200])
        self.assertEqual(t.b_list.text(), "列表发送"); self.assertEqual(self.ctx.dev.owners, {})

    def test_select_invert_move_delete_clear_and_nothing_checked(self):
        t = self.t
        for i in (1, 2, 3):
            self.fill(id_=f"{i}00"); t.b_add.click()
        t.b_inv.click(); self.assertEqual([i.checked for i in t.items], [False] * 3)
        W.QMessageBox.calls.clear(); t.b_list.click(); self.assertTrue(msgs("info"))           # nothing ticked
        t.b_all.click(); self.assertEqual([i.checked for i in t.items], [True] * 3)
        t.table.selectRow(0); t.b_down.click()
        self.assertEqual([i.can_id for i in t.items], [0x200, 0x100, 0x300]); self.assertEqual(t.table.currentRow(), 1)
        t.b_up.click(); t.b_up.click(); self.assertEqual(t.items[0].can_id, 0x100)       # second click: already on top
        t.table.selectRow(0); t.b_del.click(); self.assertEqual([i.can_id for i in t.items], [0x200, 0x300])
        t.b_clear.click(); self.assertEqual(t.items, [])

    def test_row_selection_loads_form_and_import_export(self):
        t = self.t
        self.fill(id_="18daf110", data="01 02", length="4", ext=True, count="3", itv="20", name="x", datainc=True); t.b_add.click()
        self.fill(id_="123", length="3", remote=True); t.b_add.click()
        self.assertEqual(t.table.cell_texts(1)[8], "标准远程帧")
        t.table.selectRow(0)
        self.assertEqual((t.e_id.text(), t.c_type.currentIndex(), t.e_count.text(), t.k_datainc.isChecked()), ("18daf110", 1, "3", True))
        path = os.path.join(self.ctx.dir, "send.json"); W.QFileDialog.next_path = path; t.b_exp.click()
        t.b_clear.click(); W.QFileDialog.next_path = path; t.b_imp.click()
        self.assertEqual(len(t.items), 2); self.assertTrue(t.items[1].remote); self.assertEqual(t.items[0].can_id, 0x18DAF110)
        open(path, "w", encoding="utf-8").write("{}"); W.QMessageBox.calls.clear(); t.b_imp.click()
        self.assertTrue(msgs("warn")); self.assertEqual(len(t.items), 2)

    def test_remote_frame_is_sent_as_rtr(self):
        self.fill(id_="123", length="3", remote=True); self.t.b_now.click()
        f = self._log()[0]; self.assertTrue(f.rtr); self.assertEqual((f.can_id, len(f.data)), (0x123, 3))

    def test_rows_carry_their_channel_and_go_out_on_it(self):
        self.ctx.dev.start_channel(1, ChannelCfg(), self.ctx.diag_cfg.addr)
        t = self.t
        self.fill(id_="100", count="2"); t.b_add.click()                                       # channel 0 selected -> row channel 0
        t.c_ch.setCurrentIndex(1); self.fill(id_="200", count="3"); t.b_add.click()              # channel 1 selected -> row channel 1
        self.assertEqual([it.channel for it in t.items], [0, 1])
        self.assertEqual([t.table.cell_texts(r)[2] for r in range(2)], ["0", "1"])
        t.table.item(0, 2).setText("1")                                                         # a row's channel can be changed
        self.assertEqual(t.items[0].channel, 1)
        W.QMessageBox.calls.clear(); t.table.item(0, 2).setText("5")                            # not started -> refused, reverted
        self.assertTrue(msgs("warn")); self.assertEqual(t.items[0].channel, 1)
        t.table.item(0, 2).setText("0")
        t.b_list.click()
        for _ in range(100):
            if not t.running(): break
            time.sleep(0.05)
        l0 = [f.can_id for f in self.ctx.dev.chans[0].bus._bus.log]
        l1 = [f.can_id for f in self.ctx.dev.chans[1].bus._bus.log]
        self.assertEqual(l0, [0x100] * 2); self.assertEqual(l1, [0x200] * 3)
        self.assertEqual(self.ctx.dev.owners, {})
        t.items[1].channel = -1                                                                 # an old list without channel -> current one
        t.c_ch.setCurrentIndex(0); t.b_list.click()
        for _ in range(100):
            if not t.running(): break
            time.sleep(0.05)
        self.assertEqual(len(self.ctx.dev.chans[0].bus._bus.log), 2 + 2 + 3)

    def test_fd_channel_lengths_no_remote_and_brs(self):
        self.ctx.dev.start_channel(1, ChannelCfg(protocol="CAN FD", data_kbps=2000), self.ctx.diag_cfg.addr)
        t = self.t; t.c_ch.setCurrentIndex(1)
        self.assertEqual(t.c_len.count(), 16); self.assertFalse(t.c_fmt.isEnabled())
        self.fill(length="20", data="01"); t.b_add.click()
        self.assertEqual(t.table.cell_texts(0)[4:6], ["CAN FD", "20"])
        t.b_now.click()
        f = self.ctx.dev.chans[1].bus._bus.log[0]; self.assertTrue(f.fd and f.brs); self.assertEqual(len(f.data), 20)
        t.c_len.setCurrentText("9"); t.b_add.click(); self.assertEqual(len(t.items), 1)         # 9 is not a legal FD length

    def test_no_channel_listen_only_and_busy_channel(self):
        t = self.t
        self.ctx.dev.stop_channel(0)
        self.fill(); W.QMessageBox.calls.clear(); t.b_now.click()
        self.assertIn("启动", msgs("warn")[0][2])
        self.ctx.dev.start_channel(0, ChannelCfg(listen_only=True), self.ctx.diag_cfg.addr)
        W.QMessageBox.calls.clear(); t.b_now.click(); self.assertIn("只听", msgs("warn")[0][2])
        self.ctx.dev.stop_channel(0); self.ctx.dev.start_channel(0, ChannelCfg(), self.ctx.diag_cfg.addr)
        self.ctx.dev.acquire(0, "other", self.ctx.diag_cfg.addr)
        W.QMessageBox.calls.clear(); t.b_now.click(); self.assertIn("另一个窗口", msgs("warn")[0][2])
        self.ctx.dev.release("other")

    def test_multiple_tabs_and_close(self):
        t2 = self.win.add_tab(); self.assertEqual(self.win.tabs.count(), 2)
        self.ctx.dev.start_channel(1, ChannelCfg(), self.ctx.diag_cfg.addr)
        self.assertEqual((self.t.c_ch.count(), t2.c_ch.count()), (2, 2))                      # both follow the device manager
        self.win._close_tab(1); self.assertEqual(self.win.tabs.count(), 1)
        self.win._close_tab(0); self.assertEqual(self.win.tabs.count(), 1)                    # the last tab stays


class LauncherTests(Base):
    def _fresh_launcher(self):
        self.ctx.dev.shutdown()
        return LauncherWindow(self.ctx)

    def test_agent_port_toggle(self):
        from zdiag.rpc import read_endpoint, rpc_call
        w = self._fresh_launcher()
        self.assertEqual(w.l_agent.text(), "未开启"); self.assertFalse(w.b_agent_copy.isEnabled())
        w.k_agent.click()                                                               # tick the box
        self.assertIsNotNone(w.rpc, w.l_agent.text()); self.assertTrue(w.b_agent_copy.isEnabled())
        ep = read_endpoint(self.ctx.dir); self.assertEqual(ep["url"], w.rpc.url)
        self.assertEqual(rpc_call(ep["url"], ep["token"], "device_open", {"name": SIM_DEVICE})["device"], SIM_DEVICE)
        self.assertTrue(self.ctx.dev.is_open); self.assertIn("已打开", w.l_dev_state.text())      # launcher follows
        self.assertIn("device_open", w.l_agent.text())
        W.QMessageBox.calls.clear()
        w2 = LauncherWindow(self.ctx); w2.k_agent.setChecked(True); self.assertFalse(w2.start_agent())   # a second GUI is refused
        self.assertTrue(msgs("warn")); self.assertFalse(w2.k_agent.isChecked())
        w.stop_agent(); self.assertIsNone(w.rpc); self.assertIsNone(read_endpoint(self.ctx.dir)); self.assertEqual(w.l_agent.text(), "未开启")
        self.assertTrue(self.ctx.agent_enabled)
        w.k_agent.click(); self.assertIsNotNone(w.rpc)
        w.k_agent.click(); self.assertIsNone(w.rpc); self.assertFalse(self.ctx.agent_enabled)                      # unticking is remembered
        self.ctx.save(); from zdiag_ui.appctx import AppContext; c2 = AppContext(self.ctx.dir); self.assertFalse(c2.agent_enabled); c2.dev.shutdown()
        self.ctx.agent_enabled = True; w.autostart_agent(); self.assertIsNotNone(w.rpc)                            # app start honours the choice
        w.close(); self.assertIsNone(read_endpoint(self.ctx.dir)); self.assertTrue(self.ctx.agent_enabled)        # closing stops it, keeps the choice

    def test_device_toggle_and_channel_panels(self):
        w = self._fresh_launcher()
        self.assertEqual((w.b_device.text(), w.c_device.isEnabled(), w.panels), ("打开", True, []))
        w.c_device.setCurrentText(SIM_DEVICE); w.b_device.click()
        self.assertEqual((w.b_device.text(), w.c_device.isEnabled()), ("关闭", False))   # one-way toggle, device locked
        self.assertEqual([p.isVisible() for p in w.panels], [True, True])
        self.assertEqual([p.b_toggle.text() for p in w.panels], ["启动", "启动"])
        w.b_device.click()
        self.assertEqual((w.b_device.text(), w.c_device.isEnabled()), ("打开", True))
        self.assertEqual([p.isVisible() for p in w.panels], [False, False])
        w.close()

    def test_channel_start_stop_counters_and_load(self):
        from zdiag_ui import dialogs
        w = self._fresh_launcher()
        w.c_device.setCurrentText(SIM_DEVICE); w.b_device.click()
        def fake_exec(dlg):                                       # user fills the dialog and presses 启动
            dlg.c_baud.setCurrentText("250"); dlg.c_mode.setCurrentIndex(0)
            dlg.k_load.setChecked(True); dlg.e_interval.setText("200"); dlg._on_ok(); return 1
        old = dialogs.ChannelConfigDialog.exec_; dialogs.ChannelConfigDialog.exec_ = fake_exec
        self.addCleanup(setattr, dialogs.ChannelConfigDialog, "exec_", old)
        p = w.panels[0]
        p.b_toggle.click()
        self.assertEqual((p.b_toggle.text(), self.ctx.dev.cfg_of(0).bitrate_kbps), ("停止", 250.0))
        self.assertIn("CAN 250k", p.l_state.text())
        self.assertEqual(self.ctx.chan_cfgs[0].bitrate_kbps, 250.0)                      # remembered
        from zdiag.ops import Ops
        ops = Ops(self.ctx); ops.uds_request(0, "10 03")
        w.refresh_stats()
        self.assertNotEqual(p.l_tx.text(), "发送: 0"); self.assertNotEqual(p.l_rx.text(), "接收: 0")
        self.assertEqual(p.l_err.text(), "错误: 0")
        import time; self.ctx.dev.stats_of(0).sample_load(time.monotonic())
        w.refresh_stats(); self.assertIn("总线负载", p.l_load.text())
        ops.shutdown()
        p.b_toggle.click()
        self.assertEqual((p.b_toggle.text(), p.l_rx.text(), p.l_load.text()), ("启动", "接收: 0", "总线负载: --"))
        w.close()

    def test_channel_dialog_accel_option(self):
        from zdiag_ui import dialogs
        d = dialogs.ChannelConfigDialog(SIM_DEVICE, 0, ChannelCfg(protocol="CAN FD"))
        self.assertTrue(d.c_dbaud.isEnabled()); d.c_accel.setCurrentIndex(1); d._sync()
        self.assertFalse(d.c_dbaud.isEnabled()); d._on_ok()
        self.assertFalse(d.result_cfg.fd_accel)
        d2 = dialogs.ChannelConfigDialog(SIM_DEVICE, 0, d.result_cfg)
        self.assertEqual(d2.c_accel.currentIndex(), 1)

    def test_channel_stop_and_device_close_win_over_a_window(self):
        from zdiag_ui import dialogs
        w = self._fresh_launcher()
        w.c_device.setCurrentText(SIM_DEVICE); w.b_device.click()
        old = dialogs.ChannelConfigDialog.exec_
        dialogs.ChannelConfigDialog.exec_ = lambda dlg: (dlg._on_ok(), 1)[1]
        self.addCleanup(setattr, dialogs.ChannelConfigDialog, "exec_", old)
        w.panels[1].b_toggle.click()
        class Win:
            n = 0
            def abort(self): self.n += 1
        a = Win(); self.ctx.dev.acquire(1, a, self.ctx.diag_cfg.addr)
        W.QMessageBox.calls.clear()
        w.panels[1].b_toggle.click()                                          # stopping the channel works although a window holds it
        self.assertEqual(a.n, 1); self.assertEqual(self.ctx.dev.started(), []); self.assertFalse(msgs("warn"))
        w.panels[1].b_toggle.click(); b = Win(); self.ctx.dev.acquire(1, b, self.ctx.diag_cfg.addr)
        w.b_device.click()                                                    # ... and so does closing the device
        self.assertEqual(b.n, 1); self.assertFalse(self.ctx.dev.is_open); self.assertFalse(msgs("warn"))
        w.close()

    def test_settings_button_opens_the_global_settings(self):
        from zdiag_ui import dialogs
        w = self._fresh_launcher()
        self.assertEqual(w.b_settings.text(), "设置")
        seen = []
        old = dialogs.SettingsDialog.exec_
        dialogs.SettingsDialog.exec_ = lambda dlg: (seen.append(dlg), 1)[1]
        self.addCleanup(setattr, dialogs.SettingsDialog, "exec_", old)
        w.b_settings.click()
        self.assertEqual(len(seen), 1); self.assertIn("e_key", vars(seen[0]))        # incl. the global 安全访问 key DLL
        w.close()

    def test_opens_all_tools(self):
        w = LauncherWindow(self.ctx)
        for k in ("scan", "send", "crc"):
            w._open(k)
            self.assertTrue(w.windows[k].isVisible())
        self.assertIn("zlgcan.dll", w.l_info.text())
        w.close()
        self.assertFalse(msgs("warn"))

    def test_tools_open_as_tabs_not_popups(self):
        w = LauncherWindow(self.ctx)
        self.assertEqual((w.tabs.count(), w.tabs.tabText(0)), (1, "设备管理"))
        w.b_send.click(); w.b_mon.click(); w.b_scan.click()
        self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())], ["设备管理", "普通发送", "报文监视", "软件扫描"])
        self.assertIs(w.tabs.widget(w.tabs.currentIndex()), w.windows["scan"])
        w.b_send.click()                                             # already open -> just focus its tab
        self.assertEqual((w.tabs.count(), w.tabs.currentIndex()), (4, 1))
        w.b_home.click(); self.assertEqual(w.tabs.currentIndex(), 0)
        self.assertFalse({"b_uds", "b_flash", "b_flow"} & set(vars(w)))      # 这三个界面已取消
        w.close()

    def test_close_tab(self):
        w = LauncherWindow(self.ctx); w._open("send"); w._open("monitor")
        w._close_tab_at(1)                                                       # close the 普通发送 tab
        self.assertEqual((w.tabs.count(), list(w.windows)), (2, ["monitor"]))
        w._close_tab_at(0)                                                       # the device-manager tab is permanent
        self.assertEqual(w.tabs.count(), 2)
        w.close(); self.assertEqual(w.windows, {})

    def test_float_and_dock_back(self):
        w = LauncherWindow(self.ctx); w._open("monitor"); w._open("send")
        mon = w.windows["monitor"]
        w._float_at(1)                                               # double-click / 浮动 on the 报文监视 tab
        self.assertIn("monitor", w.floating); self.assertEqual([w.tabs.tabText(i) for i in range(w.tabs.count())], ["设备管理", "普通发送"])
        self.assertTrue(mon.isVisible()); self.assertEqual(w._key_of_tab(1), "send")
        w._open("monitor"); self.assertEqual(w.tabs.count(), 2)       # focusing a floating tool does not create a tab
        w._float_at(0)                                               # the home tab cannot float
        self.assertEqual(list(w.floating), ["monitor"])
        w.dock_tool("monitor")
        self.assertEqual(([w.tabs.tabText(i) for i in range(w.tabs.count())], w.floating), (["设备管理", "普通发送", "报文监视"], {}))
        self.assertIs(w.windows["monitor"], mon); self.assertEqual(w.tabs.currentIndex(), 2)
        w.float_tool("send"); w.close_tool("send")                   # closing a floating tool removes it
        self.assertNotIn("send", w.windows); self.assertEqual(w.floating, {})
        w.float_tool("monitor"); w.close()                           # main window closes floating tools as well
        self.assertEqual((w.windows, w.floating), ({}, {}))


class MonitorTests(Base):
    def setUp(self):
        super().setUp()
        self.w = MonitorWindow(self.ctx); self.addCleanup(self.w.close)

    def rows(self):
        t = self.w.table
        return [[t.item(r, c).text() for c in range(t.columnCount())] for r in range(t.rowCount())]

    def traffic(self):
        from zdiag.ops import Ops
        ops = Ops(self.ctx); ops.uds_request(0, "10 03"); ops.shutdown()

    def test_shows_uds_traffic_overwriting_by_id(self):
        self.traffic(); self.traffic()
        self.w.refresh()
        rows = {(r[3], r[5]): r for r in self.rows()}
        req = rows[("7E2", "发送")]; rsp = rows[("7EA", "接收")]
        self.assertEqual((req[2], req[4], req[6], req[7]), ("0", "CAN", "8", "02 10 03 55 55 55 55 55"))   # tester pads 55
        self.assertEqual(req[8], "2"); self.assertGreaterEqual(int(rsp[8]), 2); self.assertTrue(rsp[7].endswith("AA"), rsp)        # ECU pads AA
        self.assertEqual(len(self.rows()), len(set((r[3], r[5]) for r in self.rows())))                  # one row per id
        self.assertEqual(self.w.l_tx.text(), "发送帧计数: 2")

    def test_delta_mode_filters_pause_and_trace(self):
        self.traffic(); w = self.w; w.refresh()
        w.c_delta.setCurrentIndex(1); w.refresh()
        self.assertTrue(all(float(r[9]) >= 0 for r in self.rows()))
        w.c_dir.setCurrentText("发送"); w.refresh(); self.assertEqual({r[5] for r in self.rows()}, {"发送"})
        w.c_dir.setCurrentText("全部"); w.e_id.setText("7ea"); w.refresh(); self.assertEqual({r[3] for r in self.rows()}, {"7EA"})
        w.e_id.setText(""); w.c_type.setCurrentText("CAN FD"); w.refresh(); self.assertEqual(self.rows(), [])
        w.c_type.setCurrentText("全部"); w.refresh(); n = len(self.rows())
        w.b_pause.click(); self.traffic(); w.refresh(); self.assertEqual(len(self.rows()), n)             # frozen
        self.assertEqual(w.b_pause.text(), "继续"); w.b_pause.click(); self.assertGreaterEqual(len(self.rows()), n)
        w.k_by_id.setChecked(False); w.refresh(); self.assertGreater(len(self.rows()), n)                # scrolling trace
        w._clear(); self.assertEqual(self.rows(), [])

    def test_error_frames_listed_and_hideable(self):
        self.ctx.dev.tap.feed_err(0); self.ctx.dev.tap.feed_err(0)
        self.w.refresh(); errs = [r for r in self.rows() if r[3] == "错误帧"]
        self.assertEqual(len(errs), 1); self.assertEqual((errs[0][7], errs[0][8], errs[0][4]), ("xxx", "2", "-"))
        self.w.k_err.setChecked(False); self.w.refresh(); self.assertEqual([r for r in self.rows() if r[3] == "错误帧"], [])

    def test_realtime_save_and_snapshot(self):
        d = tempfile.mkdtemp(); p = os.path.join(d, "live.csv"); q = os.path.join(d, "snap.csv")
        W.QFileDialog.next_path = p; self.w.b_rec.click()
        self.assertEqual(self.w.b_rec.text(), "停止保存"); self.assertIn("正在保存", self.w.l_state.text())
        self.traffic()
        self.w.b_rec.click()                                                                              # stop
        self.assertEqual(self.w.b_rec.text(), "实时保存"); self.assertTrue(msgs("info"))
        with open(p, encoding="utf-8-sig") as f:
            lines = f.read().splitlines()
        self.assertGreater(len(lines), 3); self.assertIn(",7E2,CAN,发送,8,02 10 03 55 55 55 55 55", lines[1] + "\n".join(lines))
        W.QFileDialog.next_path = q; self.w.b_save.click()
        with open(q, encoding="utf-8-sig") as f:
            self.assertGreater(len(f.read().splitlines()), 2)

    def test_launcher_button_opens_it(self):
        L = LauncherWindow(self.ctx); L._open("monitor")
        self.assertTrue(L.windows["monitor"].isVisible()); L.close_tool("monitor"); L.close()


class ScanTests(Base):
    def setUp(self):
        super().setUp()
        self.ctx.scan_dir = tempfile.mkdtemp()
        self.w = ScanWindow(self.ctx); self.addCleanup(self.w.close)

    def fields(self, i):
        return [self.w.rows[i]["fields"][k].text() for k, _ in _sw.INFO_FIELDS]

    def tiles(self):
        return [b.text() for b, _ in self.w.tile_buttons]

    def patch(self, cls, name, fn):
        old = getattr(cls, name); setattr(cls, name, fn); self.addCleanup(setattr, cls, name, old)

    def test_list_shows_four_modules_with_dashes(self):
        w = self.w
        self.assertEqual([r["name"].text() for r in w.rows], ["动力域控制器", "驱动电机控制器", "电驱充电模块", "从驱动电机控制器"])
        self.assertTrue(all(self.fields(i) == ["-"] * 4 for i in range(4)))
        self.assertEqual((w.b_start.text(), w.b_stop.text(), w.b_stop.isEnabled()), ("开始扫描", "停止扫描", False))

    def test_scan_selected_only(self):
        w = self.w
        w.rows[1]["chk"].setChecked(False); w.rows[2]["chk"].setChecked(False)
        w.b_start.click()
        self.assertTrue(all(v != "-" for v in self.fields(0)) and all(v != "-" for v in self.fields(3)))
        self.assertEqual((self.fields(1), self.fields(2)), (["-"] * 4, ["-"] * 4))      # not selected: untouched
        self.assertEqual(w.l_status.text(), "扫描完成"); self.assertEqual(self.ctx.dev.owners, {})
        self.assertEqual(w.rows[0]["fields"]["dtc"].text(), "00 02")
        w.rows[0]["chk"].setChecked(False); w.k_all.setChecked(True); w._select_all()
        self.assertTrue(all(r["chk"].isChecked() for r in w.rows))

    def test_scan_needs_started_channel_and_selection(self):
        self.ctx.dev.stop_channel(0); self.w.b_start.click()
        self.assertTrue(msgs("warn")); W.QMessageBox.calls.clear()
        self.ctx.dev.start_channel(0, ChannelCfg(), self.ctx.diag_cfg.addr, "new")
        for r in self.w.rows: r["chk"].setChecked(False)
        self.w.b_start.click(); self.assertTrue(msgs("info"))

    def test_edit_address_dialog(self):
        w = self.w
        def fake(d):
            d.e_phys.setText("7e4"); d._follow(); d.btn_ok.click(); return 1
        self.patch(_sw.ModuleAddrDialog, "exec_", fake)
        w.rows[0]["name"].click()
        m = w.store.modules()[0]
        self.assertEqual((m.phys, m.resp_id, m.func), (0x7E4, 0x7EC, 0x7DF))
        w.b_start.click(); self.assertEqual(w.rows[0]["fields"]["code"].text() != "-", True)   # scan uses the new address
        def fake2(d):
            d.e_phys.setText("7e5"); d.e_resp.setText("7f0"); d._manual(); d._follow(); d.btn_ok.click(); return 1
        self.patch(_sw.ModuleAddrDialog, "exec_", fake2)
        w.rows[0]["name"].click(); m = w.store.modules()[0]
        self.assertEqual((m.phys, m.resp_id), (0x7E5, 0x7F0))
        def bad(d):
            d.e_phys.setText("zz"); d.btn_ok.click(); return 0
        self.patch(_sw.ModuleAddrDialog, "exec_", bad); W.QMessageBox.calls.clear()
        w.rows[0]["name"].click(); self.assertTrue(msgs("warn")); self.assertEqual(w.store.modules()[0].phys, 0x7E5)

    def test_edit_info_executor_in_page_and_scan_uses_it(self):
        w = self.w
        w.rows[0]["fields"]["code"].click()
        self.assertEqual(w.stack.currentIndex(), _sw.EDITOR)
        self.assertEqual([s["pdu"] for s in w.editor.executor.steps], ["10 03", "22 F1 94"])
        self.assertFalse(w.editor.e_name.isEnabled())
        w.editor.executor.steps[-1]["pdu"] = "22 F1 90"; w.editor.on_changed()          # the VIN example from the spec
        w.editor.run()                                                                   # 试运行
        self.assertEqual(w.editor.panel.l_result.text(), "4C 53 56 41 42 31 32 33 34 35 43 30 30 30 30 30 31")
        self.assertTrue(any("22 F1 90" in l for l in w.editor.panel.log.toPlainText().split("\n")))
        self.assertTrue(w.editor.save()); self.assertEqual(w.editor.panel.l_result.text(), "已保存")
        w._editor_back(); self.assertEqual(w.stack.currentIndex(), _sw.LIST)
        w.b_start.click()
        self.assertEqual(bytes.fromhex(self.fields(0)[0]), b"LSVAB12345C000001")
        # result settings: choose step + skip
        w.rows[0]["fields"]["code"].click(); w.editor.e_skip.setText("5"); self.assertTrue(w.editor.save())
        self.assertEqual(w.store.load_info(w.modules[0], "code").result["skip"], 5)
        w.editor.e_skip.setText("x"); W.QMessageBox.calls.clear(); self.assertIsNone(w.editor.current()); self.assertTrue(msgs("warn"))

    def test_module_page_functions_run_and_navigation(self):
        w = self.w
        w.rows[0]["next"].click()
        self.assertEqual(w.stack.currentIndex(), _sw.MODULE); self.assertEqual(self.tiles(), ["故障检测", "清除故障"])
        self.assertEqual(w.l_path.text(), "动力域控制器 › 功能")
        w.tile_buttons[0][0].click()                                                     # 故障检测
        self.assertEqual(w.mpanel.l_result.text(), "01 23 45 08 01 23 45 08")
        w.tile_buttons[1][0].click(); self.assertEqual(w.mpanel.l_result.text(), "完成（无数据）")
        w.tile_buttons[0][0].click(); self.assertEqual(w.mpanel.l_result.text(), "完成（无数据）")   # list is empty now
        self.assertTrue("REQ" in w.mpanel.log.toPlainText())
        w.b_up.click(); self.assertEqual(w.stack.currentIndex(), _sw.LIST)

    def test_add_function_folder_hierarchy_like_folders(self):
        from zdiag.project import new_step
        w = self.w; w.rows[0]["next"].click()
        W.QInputDialog.next_text = ("标定", True); w.b_addfolder.click()
        self.assertEqual(self.tiles(), ["▣ 标定", "故障检测", "清除故障"])
        w.tile_buttons[0][0].click()                                                     # enter the folder
        self.assertEqual((w.l_path.text(), self.tiles()), ("动力域控制器 › 功能 › 标定", []))
        w.b_addfn.click(); self.assertEqual(w.stack.currentIndex(), _sw.EDITOR)
        w.editor.e_name.setText("读VIN"); w.editor.host.project.steps.append(new_step("service", name="读VIN", pdu="22 F1 90"))
        w.editor.on_changed(); self.assertTrue(w.editor.save())
        self.assertTrue(os.path.isfile(os.path.join(self.ctx.scan_dir, "动力域控制器", "功能", "标定", "读VIN.json")))
        w._editor_back(); self.assertEqual((w.stack.currentIndex(), self.tiles()), (_sw.MODULE, ["读VIN"]))
        w.tile_buttons[0][0].click()
        self.assertEqual(w.mpanel.l_result.text(), "4C 53 56 41 42 31 32 33 34 35 43 30 30 30 30 30 31")
        w.b_up.click(); self.assertEqual(w.l_path.text(), "动力域控制器 › 功能")             # one level up
        w.b_up.click(); self.assertEqual(w.stack.currentIndex(), _sw.LIST)
        W.QMessageBox.calls.clear(); w.rows[0]["next"].click(); W.QInputDialog.next_text = ("标定", True); w.b_addfolder.click()
        self.assertTrue(msgs("warn"))                                                    # duplicate name

    def test_manage_mode_rename_edit_delete(self):
        w = self.w; w.rows[0]["next"].click(); w.b_manage.setChecked(True); w.b_manage.click() if False else w._toggle_manage()
        pick = {"c": "rename"}
        def fake(d):
            d.choice = pick["c"]; return 1
        self.patch(_sw.ChoiceDialog, "exec_", fake)
        W.QInputDialog.next_text = ("DTC读取", True); w.tile_buttons[0][0].click()
        self.assertEqual(self.tiles(), ["DTC读取", "清除故障"])
        self.assertEqual(w.store.list_entries(w.cur_dir)[0].name, "DTC读取")
        pick["c"] = "edit"; w.tile_buttons[0][0].click()
        self.assertEqual((w.stack.currentIndex(), w.editor.e_name.text()), (_sw.EDITOR, "DTC读取"))
        w.editor.e_name.setText("故障码"); w.editor.on_changed(); self.assertTrue(w.editor.save())      # rename via the editor
        w._editor_back(); self.assertEqual(self.tiles(), ["故障码", "清除故障"])
        pick["c"] = "delete"; W.QMessageBox.next_answer = W.QMessageBox.No; w.tile_buttons[0][0].click()
        self.assertEqual(len(self.tiles()), 2)
        W.QMessageBox.next_answer = W.QMessageBox.Yes; w.tile_buttons[0][0].click(); self.assertEqual(self.tiles(), ["清除故障"])
        w.b_manage.setChecked(False); w._toggle_manage(); w.tile_buttons[0][0].click()          # normal mode runs it again
        self.assertEqual(w.mpanel.l_result.text(), "完成（无数据）")

    def test_launcher_opens_scan_window(self):
        L = LauncherWindow(self.ctx); L._open("scan")
        self.assertTrue(L.windows["scan"].isVisible()); L.close_tool("scan"); L.close()


class StepEditorTests(Base):
    def setUp(self):
        super().setUp()
        from zdiag_ui.exec_editor import ExecEditorPage
        from zdiag.scanlib import Executor
        self.page = ExecEditorPage(self.ctx, lambda ex: True, lambda ex: None, lambda: None)
        self.page.load(Executor(), "t"); self.addCleanup(self.page.close)
        self.e = self.page.editor

    def names(self):
        return [s["name"] for s in self.e.project.steps]

    def add_service(self, name):
        i = next(i for i, (sv, _) in enumerate(self.e.forms) if sv["sid"] == 0x10)
        self.e.lst.setCurrentRow(i); f = self.e.forms[i][1]
        f.e_desc.setText(name); self.e.b_add.click()

    def test_new_steps_go_below_the_selected_one(self):
        self.add_service("A"); self.add_service("B"); self.add_service("C")
        self.assertEqual(self.names(), ["A", "B", "C"])                    # nothing selected -> appended; the new step is selected
        self.assertEqual(self.e.table.currentRow(), 2)
        self.e.table.selectRow(0); self.add_service("X")
        self.assertEqual(self.names(), ["A", "X", "B", "C"]); self.assertEqual(self.e.table.currentRow(), 1)
        self.add_service("Y")                                              # continues right below the previous new step
        self.assertEqual(self.names(), ["A", "X", "Y", "B", "C"])
        self.e.table.selectRow(3); self.e.e_delay.setText("250"); self.e.b_delay.click()
        self.assertEqual([s["type"] for s in self.e.project.steps][4], "delay"); self.assertEqual(self.e.table.currentRow(), 4)
        self.assertEqual(len(self.e.project.steps), 6)

    def test_unlock_form_has_no_dll_and_uses_global_setting(self):
        i = next(i for i, (sv, _) in enumerate(self.e.forms) if sv["sid"] == 0x27)
        f = self.e.forms[i][1]
        self.assertNotIn("e_dll", vars(f))
        self.e.lst.setCurrentRow(i); f.e_level.setText("3"); self.e.b_add.click()
        st = self.e.project.steps[-1]
        self.assertEqual((st["type"], st["level"], st["dll"]), ("unlock", 3, ""))


class SecurityGlobalTests(Base):
    def test_key_dll_is_a_global_persisted_setting(self):
        cfg, ch = self.ctx.diag_cfg, self.ctx.channel
        d = SettingsDialog(self.ctx, cfg, ch)
        d.e_key.setText("C:/keys/SeedKey.dll"); d.btn_ok.click()
        self.assertEqual(self.ctx.security.key_dll, "C:/keys/SeedKey.dll")
        again = AppContext(self.ctx.dir)                                   # a new run reads it back
        self.assertEqual(again.security.key_dll, "C:/keys/SeedKey.dll")
        self.assertEqual(SettingsDialog(again, again.diag_cfg, again.channel).e_key.text(), "C:/keys/SeedKey.dll")

    def test_settings_dialog_roundtrip_and_validation(self):
        cfg, ch = self.ctx.diag_cfg, self.ctx.channel
        d = SettingsDialog(self.ctx, cfg, ch)
        self.assertEqual(d.e_phys.text(), "7e2"); self.assertEqual(d.a_p2.text(), "800")
        d.a_p2.setText("1234"); d.a_pad_on.setChecked(False); d.k_on.setChecked(True); d.k_period.setText("500")
        d.a_rxpad.setText("cc"); d.t_map.setRowCount(0); d._map_row("7e2","5","6"); d._map_row("7e8","2","4"); d._map_row("", "", "")
        d.e_sdid.setText("f1 90"); d.e_sfile.setText("Sn.txt"); d.c_sfmt.setCurrentIndex(1); d.ch_fddl.setCurrentText("32")
        d.btn_ok.click()
        self.assertEqual((cfg.settings.p2_ms, cfg.settings.padding_enabled, cfg.keepalive.period_ms), (1234, False, 500))
        self.assertEqual((cfg.settings.rx_padding, cfg.settings.padding), (0xCC, 0x55)); sec = self.ctx.security
        self.assertEqual(sec.id_map, {"7e2": [5, 6], "7e8": [2, 4]}); self.assertEqual((sec.serial_did, sec.serial_file, sec.serial_format), ("F1 90", "Sn.txt", "hex"))
        self.assertEqual((ch.fd_dl, ch.sim_profile), (32, "new"))
        d2 = SettingsDialog(self.ctx, cfg, ch)
        d2.a_p2.setText("abc"); d2.e_phys.setText("1fff")
        W.QMessageBox.calls.clear(); d2.btn_ok.click()
        self.assertTrue(msgs("warn")); self.assertEqual(cfg.settings.p2_ms, 1234)       # nothing half-applied

    def test_service_editor_changes_services(self):
        d = ServiceEditorDialog(self.ctx, None)
        d.b_add.click()
        d.e_sid.setText("BA"); d.e_name.setText("厂商服务"); d._sub_row("01", "子功能")
        d.b_ok.click()
        self.assertEqual(self.ctx.services[-1]["sid"], 0xBA); self.assertTrue(os.path.isfile(self.ctx.services_path))


class FileTransferTests(Base):
    def setUp(self):
        super().setUp()
        self.win = FileTransferWindow(self.ctx)
        self.addCleanup(lambda: (self.win.ops.ft_stop(), self.win.close()))

    def up(self, *chs):
        for c in chs:
            if c not in self.ctx.dev.started():
                self.ctx.dev.start_channel(c, ChannelCfg())
        self.win._on_dev_change()

    def test_role_switch_and_validation(self):
        w = self.win
        w.c_role.setCurrentIndex(1)
        self.assertEqual((w.role, w.b_start.text()), ("receive", "开始接收"))
        w.c_role.setCurrentIndex(0)
        self.assertEqual((w.role, w.b_start.text()), ("send", "开始发送"))
        w.b_start.click()                                                  # no channel started
        self.assertTrue(msgs("warn"))
        self.up(0)
        W.QMessageBox.calls.clear(); w.b_start.click()                    # channel ok, nothing to send
        self.assertTrue(msgs("warn")); self.assertFalse(w.running())

    def test_channel_boxes_follow_the_device(self):
        self.up(0, 1)
        self.assertEqual(sorted(self.win._boxes), [0, 1])
        self.assertEqual(self.win.selected_channels(), [0])               # first one preselected
        self.win._boxes[1].setChecked(True)
        self.assertEqual(self.win.selected_channels(), [0, 1])
        self.win._on_dev_change(); self.assertEqual(self.win.selected_channels(), [0, 1])   # the choice survives a refresh

    def test_receive_start_stop_and_close_always_works(self):
        import tempfile
        self.up(0)
        w = self.win
        w.c_role.setCurrentIndex(1)
        w.e_dest.setText(tempfile.mkdtemp()); w.k_loop.setChecked(True)
        w.b_start.click()
        self.assertTrue(w.running()); self.assertTrue(w.b_stop.isEnabled() and not w.b_start.isEnabled())
        W.QMessageBox.calls.clear()
        ev = type("E", (), {"ok": None, "accept": lambda s: setattr(s, "ok", True), "ignore": lambda s: setattr(s, "ok", False)})()
        t0 = time.monotonic()
        w.b_stop.click()                                                    # never blocks the GUI thread
        self.assertLess(time.monotonic() - t0, 1.0)
        for _ in range(100):
            if not w.running(): break
            time.sleep(0.05)
        w._tick()
        self.assertFalse(w.running()); self.assertTrue(w.b_start.isEnabled())
        self.assertEqual(self.ctx.dev.owners, {})                          # channel handed back
        w.b_start.click(); self.assertTrue(w.running())
        w.closeEvent(ev); self.assertTrue(ev.ok)                            # closing is never vetoed, it stops the transfer
        for _ in range(100):
            if not w.running(): break
            time.sleep(0.05)
        self.assertFalse(w.running()); self.assertEqual(self.ctx.dev.owners, {})

    def test_device_close_aborts_a_running_transfer(self):
        import tempfile
        self.up(0)
        w = self.win
        w.c_role.setCurrentIndex(1); w.e_dest.setText(tempfile.mkdtemp()); w.k_loop.setChecked(True); w.b_start.click()
        self.assertTrue(w.running())
        t0 = time.monotonic()
        self.ctx.dev.close()                                                # no veto, no long wait
        self.assertLess(time.monotonic() - t0, 3.0); self.assertFalse(self.ctx.dev.is_open)
        for _ in range(100):
            if not w.running(): break
            time.sleep(0.05)
        self.assertFalse(w.running())

    def test_busy_channel_is_reported(self):
        self.up(0)
        w = self.win
        tok = object(); self.ctx.dev.acquire(0, tok)                        # another window holds the channel
        w.c_role.setCurrentIndex(1); w.e_dest.setText("x"); W.QMessageBox.calls.clear(); w.b_start.click()
        self.assertTrue(msgs("warn")); self.assertFalse(w.running())
        self.ctx.dev.release(tok)


class CrcUiTests(Base):
    def test_config_widget_presets_and_custom(self):
        c = CrcConfigWidget()
        c.combo.setCurrentText("CRC32"); sp = c.spec()
        self.assertEqual((sp.name, sp.width, sp.poly), ("CRC32", 32, 0x04C11DB7))
        c.combo.setCurrentText("CRC16/MODBUS"); self.assertEqual(c.spec().name, "CRC16/MODBUS")
        c.combo.setCurrentText("自定义"); c.table.item(2, 1).setText("0x1021"); c.table.item(1, 1).setText("16")
        c.table.item(3, 1).setText("0xFFFF"); c.table.item(4, 1).setText("0"); c.chk_in.setChecked(False); c.chk_out.setChecked(False)
        self.assertEqual(c.spec().name, "CRC16/CCITT-FALSE")                  # recognised as the preset it equals
        c.table.item(2, 1).setText("zz")
        with self.assertRaises(ValueError): c.spec()

    def test_tool_dialog_per_region_and_total(self):
        p = os.path.join(tempfile.mkdtemp(), "x.hex")
        open(p, "w").write(":020000040800F2\n:0400000001020304F2\n:0400100005060708D2\n:00000001FF\n")
        d = CrcToolDialog(self.ctx); d.e_path.setText(p); d.crc.combo.setCurrentText("CRC32"); d.b_calc.click()
        self.assertEqual(d.table.rowCount(), 3)
        self.assertEqual(d.table.item(0, 1).text(), "8000000")
        self.assertEqual(d.table.item(0, 3).text(), f"{zlib.crc32(bytes([1,2,3,4])):08X}")
        self.assertEqual(d.table.item(2, 3).text(), f"{zlib.crc32(bytes([1,2,3,4,5,6,7,8])):08X}")


if __name__ == "__main__":
    unittest.main(verbosity=2)
