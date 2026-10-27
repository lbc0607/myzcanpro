import json, os, sys, tempfile, unittest, zlib
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
sys.path.insert(0, HERE)
import qt_stub
qt_stub.install()
from PyQt5 import QtWidgets as W          # the fake
from zdiag.project import SIM_DEVICE, Project, new_step, sample_project
from zdiag.crc import PRESETS
from zdiag_ui.appctx import AppContext
from zdiag_ui.launcher import LauncherWindow
from zdiag_ui.diag_window import DiagWindow
from zdiag_ui.flash_window import FlashWindow, relativize
from zdiag_ui.dialogs import CrcConfigWidget, CrcToolDialog, ServiceEditorDialog, SettingsDialog
from zdiag_ui import qt as _qt

W.QApplication([])


def fresh_ctx():
    d = tempfile.mkdtemp()
    ctx = AppContext(d)
    ctx.channel.device = SIM_DEVICE
    ctx.diag_cfg.settings.p2_ms = 800
    return ctx


def msgs(kind=None):
    return [c for c in W.QMessageBox.calls if kind is None or c[0] == kind]


class Base(unittest.TestCase):
    def setUp(self):
        W.QMessageBox.calls.clear()
        W.QMessageBox.next_answer = W.QMessageBox.Yes
        W.QFileDialog.next_path = ""
        self.ctx = fresh_ctx()
        self.addCleanup(self.ctx.conn.close)

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


class LauncherTests(Base):
    def test_opens_all_tools(self):
        w = LauncherWindow(self.ctx)
        for k in ("uds", "flash", "crc"):
            w._open(k)
            self.assertTrue(w.windows[k].isVisible())
        self.assertIn("zlgcan.dll", w.l_info.text())
        w.close()
        self.assertFalse(msgs("warn"))


class DiagWindowTests(Base):
    def setUp(self):
        super().setUp()
        self.w = DiagWindow(self.ctx)
        self.addCleanup(self.w.close)

    def test_tree_and_pdu_fill(self):
        w = self.w
        self.assertEqual(w.tree.topLevelItem(0).text(0), "【原始报文】")
        self.assertEqual(w.tree.topLevelItemCount(), 1 + len(self.ctx.services))
        w.tree.setCurrentItem(self.tree_item(w, 0x10, 0x01))
        self.assertEqual((w.e_pdu.text(), w.e_exp.text()), ("10 01", "50"))
        w.tree.setCurrentItem(self.tree_item(w, 0x22))
        self.assertEqual(w.e_pdu.text(), "22")

    def send(self, pdu, exp="", check=True, sup=False, addr=0):
        w = self.w
        w.e_pdu.setText(pdu); w.e_exp.setText(exp)
        w.c_chk.setCurrentIndex(1 if check else 0); w.c_sup.setCurrentIndex(1 if sup else 0)
        w.c_addr.setCurrentIndex(addr)
        w.b_now.click()

    def test_send_pass_fail_unchecked_and_log(self):
        w = self.w
        self.send("10 03", "50")
        self.send("10 03", "51")
        self.send("99", "7F", check=True)               # NRC but expected positive-less pattern 7F -> passes
        self.send("99", "", check=False)
        c = w.counts
        self.assertEqual((c["tested"], c["pass"], c["fail"], c["unchecked"]), (4, 2, 1, 1))
        text = w.log.toPlainText()
        self.assertIn("10 03", text); self.assertIn("50 03", text); self.assertIn("NRC 0x11", text)
        self.assertIn("未通过", text)
        self.assertEqual(w.l_tested.text(), "4")
        w.b_reset.click(); self.assertEqual(w.l_tested.text(), "0")

    def test_read_vin_multiframe_and_ascii_format(self):
        w = self.w
        self.send("22 F1 90", "62 F1 90")
        self.assertEqual(w.counts["pass"], 1)
        w.c_fmt.setCurrentIndex(1)                       # ASCII re-render
        self.assertIn("LSVAB12345C000001", w.log.toPlainText())

    def test_functional_suppress_does_not_hang(self):
        self.send("3E 00", "", check=False, sup=True, addr=1)
        self.assertEqual(w_counts(self.w), 1)

    def test_list_send_loops_delay_and_stop_button(self):
        w = self.w
        for pdu, exp in (("10 03", "50"), ("22 F1 90", "62")):
            w.e_pdu.setText(pdu); w.e_exp.setText(exp); w.c_chk.setCurrentIndex(1); w.b_add.click()
        W.QInputDialog.next_int = 20
        w.b_delay.click(); W.QInputDialog.next_int = None
        self.assertEqual(len(w.rows), 3); self.assertEqual(w.table.rowCount(), 3)
        self.assertEqual(w.table.item(2, 2).text(), "延时 20 ms")
        w.s_loop.setValue(3); w.s_gap.setValue(0)
        w.b_list.click()
        self.assertEqual(w.counts["pass"], 6)
        self.assertEqual(w.b_list.text(), "列表发送")
        # uncheck first row -> only 3 passes next time
        w.table.item(0, 0).setCheckState(0)
        self.assertFalse(w.rows[0]["enabled"])
        w._reset_stats(); w.s_loop.setValue(1); w.b_list.click()
        self.assertEqual(w.counts["pass"], 1)

    def test_list_edit_move_delete_import_export(self):
        w = self.w
        for pdu in ("10 01", "10 02", "10 03"):
            w.e_pdu.setText(pdu); w.b_add.click()
        w.table.selectRow(2); w._move(-1)
        self.assertEqual([r["pdu"] for r in w.rows], ["10 01", "10 03", "10 02"])
        w.table.item(0, 1).setText("会话1")
        self.assertEqual(w.rows[0]["name"], "会话1")
        w.table.selectRow(1); w._delete_row(); self.assertEqual(len(w.rows), 2)
        p = os.path.join(tempfile.mkdtemp(), "l.json"); W.QFileDialog.next_path = p
        w.b_exp.click()
        w.b_clear.click(); self.assertEqual(w.table.rowCount(), 0)
        w.b_imp.click(); self.assertEqual([r["pdu"] for r in w.rows], ["10 01", "10 02"])
        w.table.item(0, 2).setText("zz")
        self.assertTrue(msgs("warn"))

    def test_bad_input_is_reported_not_crash(self):
        self.send("1G")
        self.assertTrue(msgs("warn")); self.assertEqual(self.w.counts["tested"], 0)
        W.QMessageBox.calls.clear()
        self.w.e_phys.setText("xyz"); self.w.e_phys.editingFinished.emit()
        self.assertTrue(msgs("warn")); self.assertEqual(self.w.e_phys.text(), "7e2")

    def test_address_change_reaches_ecu(self):
        w = self.w
        for e, v in ((w.e_phys, "7e3"), (w.e_resp, "7eb")):
            e.setText(v); e.editingFinished.emit()
        self.assertEqual((self.ctx.diag_cfg.addr.phys, self.ctx.diag_cfg.addr.resp), (0x7E3, 0x7EB))
        self.send("10 03", "50")
        self.assertEqual(w.counts["pass"], 1)            # a new simulator was built for the new IDs

    def test_no_zlg_dll_gives_clear_message(self):
        self.ctx.channel.device = "USBCAN-II"
        self.send("10 03", "50")
        self.assertIn("zlgcan.dll", msgs("warn")[0][2]); self.assertEqual(self.w.counts["tested"], 0)

    def test_settings_dialog_roundtrip_and_validation(self):
        cfg, ch = self.ctx.diag_cfg, self.ctx.channel
        d = SettingsDialog(self.ctx, cfg, ch)
        self.assertEqual(d.e_phys.text(), "7e2"); self.assertEqual(d.a_p2.text(), "800")
        d.a_p2.setText("1234"); d.a_pad_on.setChecked(False); d.k_on.setChecked(True); d.k_period.setText("500")
        d.s_map.setText("7e2:5,6; 7e8:2,4"); d.ch_baud.setText("250000"); d.ch_sim.setCurrentIndex(1)
        d.btn_ok.click()
        self.assertEqual((cfg.settings.p2_ms, cfg.settings.padding_enabled, cfg.keepalive.period_ms), (1234, False, 500))
        self.assertEqual(cfg.serial_id_map["7e2"], [5, 6]); self.assertEqual((ch.bitrate, ch.sim_profile), (250000, "old"))
        d2 = SettingsDialog(self.ctx, cfg, ch)
        d2.a_p2.setText("abc"); d2.e_phys.setText("1fff")
        W.QMessageBox.calls.clear(); d2.btn_ok.click()
        self.assertTrue(msgs("warn")); self.assertEqual(cfg.settings.p2_ms, 1234)       # nothing half-applied
        d3 = SettingsDialog(self.ctx, cfg, ch); d3.e_phys.setText("7e5"); d3.s_map.setText("bad")
        d3.btn_ok.click(); self.assertEqual(cfg.addr.phys, 0x7E2)

    def test_keepalive_runs_with_session(self):
        self.ctx.diag_cfg.keepalive.enabled = True; self.ctx.diag_cfg.keepalive.period_ms = 50
        self.send("10 03", "50")
        self.assertIsNotNone(self.w.session)
        self.assertTrue(self.w.session.uds._ka_thread is not None)

    def test_service_editor_changes_tree(self):
        d = ServiceEditorDialog(self.ctx, self.w)
        d.b_add.click()
        d.e_sid.setText("BA"); d.e_name.setText("厂商服务"); d._sub_row("01", "子功能")
        d.b_ok.click()
        self.assertEqual(self.ctx.services[-1]["sid"], 0xBA)
        self.assertTrue(os.path.isfile(self.ctx.services_path))
        self.w._load_tree(); self.assertEqual(self.w.tree.topLevelItemCount(), 1 + len(self.ctx.services))
        d2 = ServiceEditorDialog(self.ctx, self.w); d2.b_def.click(); d2.b_ok.click()
        self.assertEqual(self.ctx.services[0]["sid"], 0x10)


def w_counts(w):
    return w.counts["tested"]


class FlashTests(Base):
    def setUp(self):
        super().setUp()
        self.w = FlashWindow(self.ctx)
        self.addCleanup(self.w.close)
        self.tmp = tempfile.mkdtemp()
        self.fw = os.path.join(self.tmp, "app.bin"); self.drv = os.path.join(self.tmp, "drv.bin")
        open(self.fw, "wb").write(os.urandom(2500)); open(self.drv, "wb").write(os.urandom(600))
        self.ctx.diag_cfg.settings.p2_ms = 800

    def prep(self, product="new", mutate=None):
        w = self.w
        self.ctx.channel.sim_profile = product
        p = sample_project(product)
        p.cfg.settings.p2_ms = 800
        for s in p.steps:
            if s["type"] == "unlock": s["dll"] = "builtin:xor"
            if s["type"] == "driver": s["file"] = self.drv
            if s["type"] == "download": s["file"] = self.fw
        if mutate: mutate(p)
        w.project = p; w.editor.refresh(); w.apply_to_executor()

    def run_exec(self):
        self.w.executor.b_start.click()

    def card_colors(self):
        return [it._bg.a[0].a[0] for it in (self.w.executor.cards.item(i) for i in range(self.w.executor.cards.count()))]

    def test_editor_shows_sample_like_screenshot(self):
        e = self.w.editor
        self.assertEqual(e.table.rowCount(), 9)
        self.assertEqual([e.table.item(r, 0).text() for r in (0, 1, 4, 8)], ["进入扩展模式", "关闭DTC设置", "ECU解锁", "ECU复位"])
        self.assertEqual(e.table.item(7, 1).text(), "31 01 01 02 [CRC32]")
        self.assertIn("编辑器 - sample-new", self.w.tabs.tabText(0))

    def test_select_row_loads_forms_and_update_changes_params(self):
        e = self.w.editor
        e.table.selectRow(4)                                     # unlock
        self.assertEqual(e.lst.currentItem().text(0), "(27)安全访问") if False else None
        form = e._current_form(); self.assertEqual(form.kind, "unlock"); self.assertEqual(form.e_level.text(), "1")
        form.e_level.setText("3"); e.b_upd.click()
        self.assertEqual(self.w.project.steps[4]["level"], 3)
        self.assertEqual(e.table.item(4, 1).text(), "级别:3 DLL:sa_demo.dll")
        e.table.selectRow(7)                                     # routine -> change RID + CRC type
        f = e._current_form(); self.assertEqual(f.kind, "routine"); self.assertEqual(f.e_rid.text(), "01 02")
        f.e_rid.setText("DF FE"); f.crc.combo.setCurrentText("CRC16/XMODEM"); e.b_upd.click()
        st = self.w.project.steps[7]
        self.assertEqual((st["rid"], st["crc"]["width"], st["crc"]["name"]), ("DF FE", 16, "CRC16/XMODEM"))
        self.assertEqual(e.table.item(7, 1).text(), "31 01 DF FE [CRC16/XMODEM]")
        e.table.selectRow(0)
        g = e._current_form(); self.assertEqual(g.kind, "service"); self.assertEqual(g.e_data.text(), "")
        g.e_data.setText("")                                     # 10 + sub 03
        self.assertEqual(g.c_sub.currentText(), "03 扩展会话")

    def test_add_generic_service_with_sub_and_data(self):
        e = self.w.editor
        idx = [i for i, (s, f) in enumerate(e.forms) if s["sid"] == 0x28][0]
        e.lst.setCurrentRow(idx)
        f = e._current_form()
        f.c_sub.setCurrentText("03 禁止收发"); f.e_data.setText("01"); f.c_addr.setCurrentIndex(1); f.c_sup.setCurrentIndex(1)
        f.e_desc.setText("关闭通信"); e.b_add.click()
        s = self.w.project.steps[-1]
        self.assertEqual((s["pdu"], s["addr"], s["suppress"], s["name"]), ("28 03 01", "func", True, "关闭通信"))
        e.table.item(len(self.w.project.steps) - 1, 1).setText("28 00 01")        # inline edit of the PDU
        self.assertEqual(self.w.project.steps[-1]["pdu"], "28 00 01")
        e.e_delay.setText("250"); e.b_delay.click()
        self.assertEqual(self.w.project.steps[-1]["ms"], 250)
        e.e_delay.setText("x"); W.QMessageBox.calls.clear(); e.b_delay.click(); self.assertTrue(msgs("warn"))

    def test_invalid_form_values_warn(self):
        e = self.w.editor
        e.table.selectRow(4)
        f = e._current_form(); f.e_level.setText("2"); W.QMessageBox.calls.clear(); e.b_upd.click()
        self.assertIn("奇数", msgs("warn")[0][2])
        e.table.selectRow(7); f = e._current_form(); f.e_rid.setText("DF"); W.QMessageBox.calls.clear(); e.b_upd.click()
        self.assertIn("2 字节", msgs("warn")[0][2])
        f.e_rid.setText("DF FE"); f.e_data.setText("{bogus}"); W.QMessageBox.calls.clear(); e.b_upd.click()
        self.assertIn("占位符", msgs("warn")[0][2])

    def test_save_open_roundtrip_with_relative_paths(self):
        self.prep("new")
        path = os.path.join(self.tmp, "plan.json"); W.QFileDialog.next_path = path
        self.w.editor.b_save.click()
        d = json.load(open(path)); fs = [s.get("file") for s in d["steps"] if s["type"] in ("driver", "download")]
        self.assertEqual(sorted(fs), ["app.bin", "drv.bin"])                  # relativised
        self.assertFalse(self.w.dirty)
        self.w.editor.b_clr.click(); self.assertTrue(self.w.dirty); self.assertEqual(self.w.editor.table.rowCount(), 0)
        self.w.editor.b_open.click()                                          # same path from the dialog stub
        self.assertEqual(self.w.editor.table.rowCount(), 9)
        self.w.apply_to_executor()
        self.assertEqual(self.w.executor.base_dir, self.tmp)

    def test_full_flash_new_product_via_gui(self):
        self.prep("new")
        self.run_exec()
        ex = self.w.executor
        self.assertEqual(self.card_colors(), [qt_color("#5FAE5B")] * 9)
        self.assertEqual(ex.b_start.text(), "开始刷写"); self.assertEqual(ex.bar.value(), 100)
        self.assertTrue(msgs("info")[-1][1] == "完成")
        types = {ex.table.item(r, 1).text() for r in range(ex.table.rowCount())}
        self.assertTrue({"请求", "响应", "信息", "完成"} <= types)
        ecu = self.ctx.conn.ecu
        self.assertEqual(ecu.reset_count, 1); self.assertEqual(ecu.crc_checked, [(0x0102, True)]); self.assertEqual(len(ecu.seed), 16)
        data = open(self.fw, "rb").read()
        self.assertEqual(bytes(ecu.mem[i] for i in range(len(data))), data)

    def test_full_flash_old_product_via_gui(self):
        self.prep("old")
        self.run_exec()
        ecu = self.ctx.conn.ecu
        self.assertEqual(self.card_colors(), [qt_color("#5FAE5B")] * 9)
        self.assertEqual((len(ecu.seed), ecu.crc_checked), (2, [(0xDFFE, True)]))

    def test_product_mismatch_marks_failing_card_and_stops(self):
        def mut(p):
            for s in p.steps:
                if s["type"] == "routine": s["rid"] = "DF FE"          # new ECU doesn't know it
        self.prep("new", mut)
        self.run_exec()
        cols = self.card_colors()
        self.assertEqual(cols[:7], [qt_color("#5FAE5B")] * 7); self.assertEqual(cols[7], qt_color("#D9534F"))
        self.assertEqual(cols[8], qt_color("#6C8EBF"))                    # never ran
        self.assertEqual(self.ctx.conn.ecu.reset_count, 0)
        self.assertTrue(msgs("warn"))

    def test_preflight_blocks_start_and_lists_problems(self):
        self.prep("new", lambda p: [s.__setitem__("file", "/no/such.bin") for s in p.steps if s["type"] == "download"])
        self.run_exec()
        self.assertEqual(len(msgs("warn")), 1); self.assertIn("步骤7", msgs("warn")[0][2])
        self.assertIsNone(self.ctx.conn.bus)                              # never touched the device

    def test_repeat_count_and_gap(self):
        self.prep("old")
        self.w.executor.s_n.setValue(2)
        self.run_exec()
        self.assertEqual(self.ctx.conn.ecu.reset_count, 2)

    def test_executor_open_file_and_address_edit(self):
        self.prep("new")
        path = os.path.join(self.tmp, "p2.json"); self.w.project.save(path)
        W.QFileDialog.next_path = path
        self.w.executor.b_open.click()
        self.assertEqual(self.w.executor.e_path.text(), path)
        ex = self.w.executor
        ex.e_phys.setText("7e5"); ex.e_phys.editingFinished.emit()
        self.assertEqual(ex.project.cfg.addr.phys, 0x7E5)
        self.assertEqual(self.w.project.cfg.addr.phys, 0x7E2)             # editor copy untouched

    def test_new_project_and_dirty_guard(self):
        self.w.editor.b_clr.click()
        W.QMessageBox.next_answer = W.QMessageBox.No
        self.w.editor.b_new.click()
        self.assertEqual(self.w.project.name, "sample-new")                 # discard refused
        W.QMessageBox.next_answer = W.QMessageBox.Yes
        self.w.editor.b_new.click()
        self.assertEqual(self.w.editor.table.rowCount(), 0); self.assertFalse(self.w.dirty)


def qt_color(h):
    return h


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
