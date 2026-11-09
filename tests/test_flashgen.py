"""Trace file reading, ISO-TP reassembly, UDS pairing and the draft 功能 generated from a flashing session."""
import os, sys, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.flashgen import draft
from zdiag.tracefile import Frame, pair, parse_frames, reassemble

TESTER, ECU = 0x7E2, 0x7EA


def isotp(can_id, payload, t, fc_from=None):
    """Frames of one ISO-TP message (8-byte frames, 0x55 padding); returns [(t, id, data)]."""
    out = []
    if len(payload) <= 7:
        out.append((t, can_id, bytes([len(payload)]) + payload + b"\x55" * (7 - len(payload))))
        return out
    out.append((t, can_id, bytes([0x10 | (len(payload) >> 8), len(payload) & 0xFF]) + payload[:6]))
    if fc_from is not None:
        out.append((t + 0.0005, fc_from, bytes([0x30, 0, 0]) + b"\x55" * 5))
    rest, sn, k = payload[6:], 1, 0
    while rest:
        chunk = rest[:7]; rest = rest[7:]
        k += 1
        out.append((t + 0.001 * (k + 1), can_id, bytes([0x20 | sn]) + chunk + b"\x55" * (7 - len(chunk))))
        sn = (sn + 1) & 0xF
    return out


class Session:
    def __init__(self):
        self.t, self.frames = 1.0, []

    def ex(self, req, resp, gap=0.01, pending=0, tester=TESTER, ecu=ECU):
        self.t += gap
        self.frames += isotp(tester, bytes(req), self.t, fc_from=ecu)
        self.t += 0.004
        for _ in range(pending):
            self.frames += isotp(ecu, bytes([0x7F, req[0], 0x78]), self.t); self.t += 0.2
        if resp is not None:
            self.frames += isotp(ecu, bytes(resp), self.t, fc_from=tester)
        return self

    def noise(self, n=20):                                         # cyclic application frames that must be ignored
        for k in range(n):
            self.frames.append((1.0 + k * 0.1, 0x321, bytes([k, 2, 3, 4, 5, 6, 7, 8])))
        return self

    def flash(self):
        s = self
        s.ex([0x10, 0x03], [0x50, 0x03, 0, 0x32, 1, 0xF4])
        s.ex([0x27, 0x01], [0x67, 0x01, 0xAA, 0xBB, 0xCC, 0xDD]).ex([0x27, 0x02, 1, 2, 3, 4], [0x67, 0x02])
        s.ex([0x34, 0x00, 0x44, 0x20, 0x00, 0x00, 0x00, 0x00, 0x00, 0x00, 0x20], [0x74, 0x20, 0x04, 0x02])
        s.ex([0x36, 0x01] + [7] * 16, [0x76, 0x01]).ex([0x36, 0x02] + [8] * 16, [0x76, 0x02]).ex([0x37], [0x77])
        s.ex([0x31, 0x01, 0xFF, 0x00, 0x44, 0x00, 0x01, 0x00, 0x00, 0x00, 0x00, 0x10, 0x00], [0x71, 0x01, 0xFF, 0x00], gap=1.2, pending=2)
        for addr, size in ((0x10000, 40), (0x20000, 24)):
            s.ex([0x34, 0x00, 0x44] + list(addr.to_bytes(4, "big")) + list(size.to_bytes(4, "big")), [0x74, 0x20, 0x04, 0x02])
            for blk, off in enumerate(range(0, size, 20), 1):
                s.ex([0x36, blk] + [blk] * min(20, size - off), [0x76, blk])
            s.ex([0x37], [0x77]).ex([0x31, 0x01, 0xDF, 0xFE, 1, 2, 3, 4], [0x71, 0x01, 0xDF, 0xFE])
        s.ex([0x31, 0x01, 0x02, 0x02, 9, 9, 9, 9], [0x71, 0x01, 0x02, 0x02])
        s.ex([0x31, 0x01, 0xFF, 0x01], [0x71, 0x01, 0xFF, 0x01])
        s.ex([0x3E, 0x80], None)
        s.ex([0x11, 0x01], [0x51, 0x01]).ex([0x22, 0xF1, 0x95], [0x62, 0xF1, 0x95, 0x31], gap=3.0)
        return s

    def csv(self, header="序号,时间标识(s),通道,帧ID,CAN类型,方向,长度,数据"):
        rows = [header]
        for i, (t, cid, d) in enumerate(sorted(self.frames, key=lambda f: f[0]), 1):
            rows.append(f"{i},{t:.6f},0,{cid:X},标准帧,{'发送' if cid == TESTER else '接收'},{len(d)},{' '.join(f'{b:02X}' for b in d)}")
        return "\n".join(rows) + "\n"

    def asc(self):
        rows = ["date Sat Nov 8 2026", "base hex  timestamps absolute", "no internal events logged"]
        for t, cid, d in sorted(self.frames, key=lambda f: f[0]):
            rows.append(f"   {t:.6f} 1  {cid:X}             {'Tx' if cid == TESTER else 'Rx'}   d {len(d)} {' '.join(f'{b:02X}' for b in d)}")
        return "\n".join(rows) + "\n"

    def table_us(self):
        rows = ["序号\t传输方向\t时间标识\t帧ID\t帧格式\t帧类型\t数据长度\t数据"]
        for i, (t, cid, d) in enumerate(sorted(self.frames, key=lambda f: f[0]), 1):
            rows.append(f"{i}\t{'发送' if cid == TESTER else '接收'}\t{int(t * 1e6)}\t0x{cid:08X}\t数据帧\t标准帧\t{len(d)}\t{''.join(f'{b:02X}' for b in d)}")
        return "\n".join(rows) + "\n"


def run(text, **kw):
    frames, info = parse_frames(text, **kw)
    ex, pinfo = pair(reassemble(frames))
    return frames, info, ex, pinfo


class Parse(unittest.TestCase):
    def test_three_formats_give_the_same_exchanges(self):
        s = Session().flash().noise()
        ref = None
        for name, text in (("csv", s.csv()), ("asc", s.asc()), ("table", s.table_us())):
            frames, info, ex, pinfo = run(text)
            self.assertEqual(len(frames), len(s.frames), name)
            self.assertEqual(info["time_unit"], "us" if name == "table" else "s", name)
            sig = [(e.req, e.resp) for e in ex]
            self.assertEqual(sig, ref or sig); ref = sig
            self.assertEqual(pinfo["pairs"][0]["request_id"], "7E2"); self.assertEqual(pinfo["pairs"][0]["response_id"], "7EA")
        self.assertIn("vector asc", run(s.asc())[1]["format"])

    def test_isotp_multiframe_pending_and_unanswered(self):
        s = Session().ex([0x2E, 0xF1, 0x90] + [0x41] * 17, [0x6E, 0xF1, 0x90]).ex([0x31, 0x01, 0xFF, 0x00, 1], [0x71, 0x01, 0xFF, 0x00], pending=3).ex([0x3E, 0x80], None)
        _, _, ex, _ = run(s.csv())
        self.assertEqual(len(ex[0].req), 20); self.assertTrue(ex[0].ok)
        self.assertEqual(ex[1].pending, 3); self.assertTrue(ex[1].ok); self.assertGreater(ex[1].t_resp - ex[1].t_req, 0.5)
        self.assertTrue(ex[2].suppressed and ex[2].resp is None)

    def test_negative_response_and_filtering(self):
        s = Session().ex([0x22, 0xF1, 0x90], [0x7F, 0x22, 0x31])
        s.ex([0x22, 0xF1, 0x90], [0x62, 0xF1, 0x90, 0x41], tester=0x745, ecu=0x74D)
        _, _, ex, p = run(s.csv()); self.assertEqual(len(ex), 2); self.assertEqual(ex[0].nrc, 0x31)
        frames, _ = parse_frames(s.csv()); msgs = reassemble(frames)
        ex1, _ = pair(msgs, tester_id=0x745); self.assertEqual([e.req_id for e in ex1], [0x745])
        ex2, p2 = pair(msgs, ecu_id=0x999); self.assertEqual(ex2, []); self.assertTrue(p2["warnings"])

    def test_plain_lines_and_garbage(self):
        txt = "7E2 02 10 03 55 55 55 55 55\n7EA 06 50 03 00 32 01 F4 55\n\nnot a frame\n0.5 Rx 7E2 [8] 02 3E 80 00 00 00 00 00\n"
        frames, info = parse_frames(txt); self.assertEqual(len(frames), 3); self.assertEqual(info["skipped"], 1)
        _, info2 = parse_frames("hello\nworld\n"); self.assertTrue(info2["warnings"])
        with self.assertRaises(ValueError): parse_frames(txt, time_unit="h")
        f3, _ = parse_frames("序号,时间标识(ms),帧ID,数据\n1,1500,7E2,02 10 03\n"); self.assertAlmostEqual(f3[0].ts, 1.5)


class Draft(unittest.TestCase):
    def setUp(self):
        _, _, ex, _ = run(Session().flash().noise().csv())
        self.d = draft(ex)
        self.steps = self.d["steps"]

    def kinds(self):
        return [(s["type"], s.get("rkind", "")) for s in self.steps]

    def test_structure(self):
        self.assertEqual(self.kinds(), [("service", ""), ("unlock", ""), ("driver", ""), ("delay", ""), ("routine", "erase"),
                                        ("download", ""), ("routine", "crc"), ("routine", "custom"), ("service", ""), ("delay", ""), ("service", "")])

    def test_step_details(self):
        s = self.steps
        self.assertEqual((s[0]["pdu"], s[0]["addr"], s[0]["name"]), ("10 03", "phys", "会话控制 扩展会话"))
        self.assertEqual(s[1]["level"], 1)
        self.assertEqual((s[2]["base_addr"], s[2]["addr_len"], s[2]["size_len"], s[2]["dfi"]), ("20000000", 4, 4, "00"))
        self.assertEqual(s[3]["ms"], 1200)
        self.assertEqual((s[4]["rid"], s[4]["addr_len"], s[4]["size_len"], s[4]["ext_on"], s[4]["ext"]), ("FF 00", 4, 4, False, "01"))
        self.assertEqual(s[5]["base_addr"], "10000")                                           # two segments -> ONE download step
        self.assertEqual((s[6]["rid"], s[6]["rid_file"], s[6]["crc_mode"]), ("DF FE", "02 02", "region"))
        self.assertEqual((s[7]["rid"], s[7]["sub"], s[7]["data"]), ("FF 01", "01", ""))
        self.assertEqual(s[8]["pdu"], "11 01"); self.assertEqual(s[9]["ms"], 3000); self.assertEqual(s[10]["pdu"], "22 F1 95")
        self.assertFalse([x for x in s if x.get("pdu", "").startswith("3E")])

    def test_todo_notes_and_validity(self):
        todo = " ".join(self.d["todo"])
        for w in ("密钥 DLL", "Flash driver", "文件下载", "CRC", "0x10000/40字节", "0x20000/24字节"): self.assertIn(w, todo)
        self.assertTrue(any("TesterPresent" in n for n in self.d["notes"]))
        self.assertTrue(any("擦除范围" in n for n in self.d["notes"]))
        from zdiag.project import STEP_TYPES
        for s in self.steps: self.assertIn(s["type"], STEP_TYPES)
        self.assertEqual(self.d["warnings"], [])

    def test_warnings_for_problem_sessions(self):
        s = Session().ex([0x10, 0x03], [0x7F, 0x10, 0x22]).ex([0x34, 0x00, 0x44, 0, 0, 0, 0, 0, 0, 0, 8], [0x74, 0x20, 4, 2]).ex([0x36, 1, 1, 2, 3], [0x76, 1])
        d = draft(run(s.csv())[2])
        w = " ".join(d["warnings"]); self.assertIn("NRC 0x22", w); self.assertIn("退出传输", w); self.assertIn("声明长度 8", w)
        s2 = Session().ex([0x27, 0x01], [0x67, 0x01, 1, 2]); self.assertIn("27 02", " ".join(draft(run(s2.csv())[2])["warnings"]))

    def test_single_download_and_no_erase(self):
        s = Session().ex([0x34, 0x00, 0x44, 0, 1, 0, 0, 0, 0, 0, 3], [0x74, 0x20, 4, 2]).ex([0x36, 1, 1, 2, 3], [0x76, 1]).ex([0x37], [0x77])
        st = draft(run(s.csv())[2])["steps"]; self.assertEqual([x["type"] for x in st], ["download"])
        s.ex([0x34, 0x00, 0x44, 0, 2, 0, 0, 0, 0, 0, 3], [0x74, 0x20, 4, 2]).ex([0x36, 1] + [1] * 3, [0x76, 1]).ex([0x37], [0x77])
        d = draft(run(s.csv())[2]); self.assertEqual([x["type"] for x in d["steps"]], ["download"])   # back-to-back segments = one image
        self.assertIn("0x10000/3字节, 0x20000/3字节", " ".join(d["todo"]))




def record_sim_flash(extra_cleanup=None):
    """Flash the simulated ECU with the built-in sample plan and return (csv text of the bus, plan, files dir, fw, drv)."""
    import tempfile
    from zdiag.busstats import ChannelCfg
    from zdiag.engine import Hooks
    from zdiag.project import SIM_DEVICE, sample_project
    from zdiag_ui.appctx import AppContext
    ctx = AppContext(tempfile.mkdtemp())
    try:
        ctx.dev.open(SIM_DEVICE)
        ctx.dev.start_channel(0, ChannelCfg(), ctx.diag_cfg.addr, "new", 0)
        d = tempfile.mkdtemp()
        fw, drv = os.path.join(d, "fw.bin"), os.path.join(d, "drv.bin")
        for pth, n in ((fw, 3000), (drv, 700)):
            with open(pth, "wb") as f: f.write(os.urandom(n))
        plan = sample_project("new")
        for st in plan.steps:
            if st["type"] == "unlock": st["dll"] = "builtin:xor"
            if st["type"] == "driver": st["file"] = drv
            if st["type"] == "download": st["file"] = fw
        ctx.diag_cfg.settings.p2_ms = 1500
        tok = object(); sess = ctx.open_session(ctx.diag_cfg, ctx.channel, tok, Hooks())
        try:
            ok = sess.run_plan(plan.steps)
            if not ok: raise AssertionError(sess.last_error)
        finally:
            sess.close(); ctx.dev.release(tok)
        tap = ctx.dev.tap
        rows = tap.trace(tap.RING)
        text = "序号,时间标识(s),通道,帧ID,CAN类型,方向,长度,数据\n" + "\n".join(
            f"{r.seq},{r.ts - tap.t0:.6f},{r.ch},{r.id_text},标准帧,{r.dir},{len(r.data)},{r.data_text}" for r in rows)
        return text, plan, d, fw, drv
    finally:
        ctx.dev.shutdown()


class RoundTrip(unittest.TestCase):
    """Flash the simulated ECU with the built-in sample plan, record the bus, regenerate the plan from the recording and run
    the regenerated plan again."""

    def test_record_regenerate_and_rerun(self):
        import copy
        text, plan, d, fw, drv = record_sim_flash()
        frames, info, ex, pinfo = run(text)
        self.assertGreater(len(ex), 10, pinfo)
        dr = draft(ex)
        orig = [(s["type"], s.get("rkind", "")) for s in plan.steps if not (s["type"] == "service" and s["pdu"].upper().startswith("3E"))]
        got = [(s["type"], s.get("rkind", "")) for s in dr["steps"] if s["type"] != "delay"]
        self.assertEqual(got, orig, dr["warnings"])
        # fill in what a trace cannot tell (files, key DLL, CRC parameters) and run the regenerated plan on a fresh simulated ECU
        steps = copy.deepcopy(dr["steps"])
        crc = [s for s in plan.steps if s.get("rkind") == "crc"][0]
        for s in steps:
            if s["type"] == "unlock": s["dll"] = "builtin:xor"
            if s["type"] == "driver": s["file"] = drv
            if s["type"] == "download": s["file"] = fw
            if s.get("rkind") == "crc":
                for k in ("crc", "crc_order", "crc_mode", "rid", "rid_file"): s[k] = crc.get(k, s.get(k))
        from zdiag.engine import preflight
        self.assertEqual(preflight(steps, d), [])
        from zdiag.sim_ecu import make_sim
        from zdiag.engine import DiagSession, Hooks
        from zdiag.project import DiagConfig
        cfg = DiagConfig(); cfg.settings.p2_ms = 1500
        bus, ecu = make_sim(cfg.addr, "new")
        s2 = DiagSession(bus, cfg, Hooks())
        try:
            self.assertTrue(s2.run_plan([x for x in steps if x["type"] != "delay"]), s2.last_error)
        finally:
            s2.close(); ecu.stop()


if __name__ == "__main__":
    unittest.main(verbosity=2)
