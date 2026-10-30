import os, sys, tempfile, threading, unittest, zlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.crc import PRESETS, CrcSpec, crc_bytes
from zdiag.engine import DiagSession, Hooks, Reply, StepError, match_expect, render_template, default_expect
from zdiag.project import DiagConfig, Project, new_step, sample_project, parse_hex, step_summary
from zdiag.sim_ecu import make_sim
from zdiag.flash import Segment


def tmpfile(data, name="fw.bin"):
    d = tempfile.mkdtemp(); p = os.path.join(d, name); open(p, "wb").write(data); return p


def make_plan(product, fw_path, drv_path):
    p = sample_project(product)
    for s in p.steps:
        if s["type"] == "unlock": s["dll"] = "builtin:xor"
        if s["type"] == "driver": s["file"] = drv_path
        if s["type"] == "download": s["file"] = fw_path
    return p


def run(product, plan_mutator=None, cfg_mutator=None, **simkw):
    cfg = DiagConfig()
    cfg.settings.p2_ms = 1500
    if cfg_mutator: cfg_mutator(cfg)
    bus, ecu = make_sim(cfg.addr, product, **simkw)
    fw, drv = os.urandom(3000), os.urandom(700)
    plan = make_plan(product, tmpfile(fw), tmpfile(drv, "drv.bin"))
    if plan_mutator: plan_mutator(plan)
    events, steps = [], {}
    h = Hooks(); h.log = events.append; h.step = lambda i, s, d: steps.__setitem__(i, (s, d))
    sess = DiagSession(bus, cfg, h)
    try:
        ok = sess.run_plan(plan.steps)
    finally:
        sess.close(); ecu.stop()
    return ok, ecu, steps, events, fw, drv


class ProductFlows(unittest.TestCase):
    def check_full(self, product, rid, seed_len, rid_file):
        ok, ecu, steps, ev, fw, drv = run(product)
        self.assertTrue(ok, steps)
        self.assertEqual(ecu.seed_len, seed_len)
        self.assertEqual(len(ecu.seed), seed_len)              # seed length as the ECU generation dictates
        self.assertEqual(ecu.crc_checked, [(rid, True), (rid_file, True)])   # 分段CRC then 全文件CRC
        self.assertEqual(ecu.reset_count, 1)
        self.assertTrue(all(v[0] == "ok" for v in steps.values()) and len(steps) == 10)
        self.assertEqual(bytes(ecu.mem[0 + i] for i in range(len(fw))), fw)   # last 34 target addr 0

    def test_new_product_16byte_seed_and_0102(self): self.check_full("new", 0x0102, 16, 0x0103)
    def test_old_product_2byte_seed_and_DFFE(self):   self.check_full("old", 0xDFFE, 2, 0xDFFD)

    def test_wrong_crc_algorithm_stops_before_reset(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["crc"] = PRESETS["CRC32/MPEG-2"].to_dict()
        ok, ecu, steps, ev, *_ = run("new", mut)
        self.assertFalse(ok)
        self.assertEqual(steps[7][0], "fail"); self.assertIn("0x72", steps[7][1]); self.assertIn("分段CRC", steps[7][1])  # caught right after the segment
        self.assertEqual(ecu.reset_count, 0); self.assertNotIn(8, steps)

    def test_wrong_rid_for_product_fails(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["rid"] = "DF FE"         # new ECU does not know it
        ok, ecu, steps, *_ = run("new", mut)
        self.assertFalse(ok); self.assertEqual(steps[7][0], "fail")

    def test_unlock_needs_odd_level(self):
        def mut(plan):
            for s in plan.steps:
                if s["type"] == "unlock": s["level"] = 2
        ok, ecu, steps, *_ = run("new", mut)
        self.assertFalse(ok); self.assertEqual(steps[4][0], "fail")

    def test_crc_without_download_step(self):
        def mut(plan): plan.steps = [s for s in plan.steps if s["type"] != "download"]
        ok, ecu, steps, *_ = run("new", mut)
        self.assertFalse(ok)
        self.assertIn("文件下载", [v for k, v in steps.items() if v[0] == "fail"][0][1])

    def test_cancel_during_download(self):
        cfg = DiagConfig()
        bus, ecu = make_sim(cfg.addr, "new")
        plan = make_plan("new", tmpfile(os.urandom(20000)), tmpfile(os.urandom(100), "d.bin"))
        h = Hooks(); steps = {}
        h.step = lambda i, s, d: steps.__setitem__(i, (s, d))
        sess = DiagSession(bus, cfg, h)
        h.progress = lambda d, t: sess.cancel.set() if d > 2000 else None
        try:
            self.assertFalse(sess.run_plan(plan.steps))
        finally: sess.close(); ecu.stop()
        self.assertEqual(steps[7], ("fail", "已取消")); self.assertEqual(ecu.reset_count, 0)

    def test_repeat_runs(self):
        cfg = DiagConfig(); bus, ecu = make_sim(cfg.addr, "old")
        plan = make_plan("old", tmpfile(os.urandom(500)), tmpfile(os.urandom(100), "d.bin"))
        sess = DiagSession(bus, cfg)
        try: self.assertTrue(sess.run_plan(plan.steps, repeat=2, interval_s=0.05))
        finally: sess.close(); ecu.stop()
        self.assertEqual(ecu.reset_count, 2); self.assertEqual(len(ecu.crc_checked), 4)       # (分段 + 全文件) x 2 runs

    def test_events_show_requests_and_multiframe_unlock(self):
        ok, ecu, steps, ev, *_ = run("new", cfg_mutator=lambda c: setattr(c.settings, "show_raw", True))
        self.assertTrue(ok)
        reqs = [e.data.hex(" ") for e in ev if e.kind == "REQ"]
        self.assertIn("10 03", reqs); self.assertIn("11 01", reqs)
        keyreq = [e for e in ev if e.kind == "REQ" and e.data[:2] == b"\x27\x02"][0]
        self.assertEqual(len(keyreq.data), 18)                       # 16-byte key -> multi-frame
        raw_tx = [e for e in ev if e.kind == "CAN" and e.direction == "TX" and e.data[0] >> 4 == 1]
        self.assertTrue(raw_tx)                                      # a FirstFrame went out


class ServiceStepSemantics(unittest.TestCase):
    def sess(self):
        cfg = DiagConfig(); cfg.settings.p2_ms = 400
        bus, ecu = make_sim(cfg.addr, "new", chatter=False)
        s = DiagSession(bus, cfg); self.addCleanup(lambda: (s.close(), ecu.stop())); return s, cfg

    def test_expect_ok_and_mismatch(self):
        s, _ = self.sess()
        s.run_step(new_step("service", pdu="10 03", expect="50 03"))
        with self.assertRaises(StepError): s.run_step(new_step("service", pdu="10 03", expect="50 01"))
        s.run_step(new_step("service", pdu="10 03", expect="50 xx"))

    def test_nrc_fails_checked_but_not_unchecked(self):
        s, _ = self.sess()
        with self.assertRaises(StepError): s.run_step(new_step("service", pdu="99"))
        s.run_step(new_step("service", pdu="99", check=False))       # NRC accepted when not checked

    def test_expect_negative_with_any(self):
        s, cfg = self.sess()
        with self.assertRaises(StepError): s.run_step(new_step("service", pdu="99", expect="7F 99 12"))
        cfg.settings.any_negative = True
        s.run_step(new_step("service", pdu="99", expect="7F"))

    def test_suppress_and_timeout(self):
        s, _ = self.sess()
        s.run_step(new_step("service", pdu="3E 00", suppress=True))
        s.ecu_dead = True

    def test_functional_addressing_uses_7df(self):
        s, cfg = self.sess()
        s.tap.enabled = True; evs = []; s.hooks.log = evs.append; s.tap.report = evs.append
        s.exchange(parse_hex("3E 80"), "func", True)
        self.assertEqual([e.can_id for e in evs if e.kind == "CAN" and e.direction == "TX"], [0x7DF])
        s.exchange(parse_hex("3E 80"), "phys", True)
        self.assertEqual([e.can_id for e in evs if e.kind == "CAN" and e.direction == "TX"][-1], 0x7E2)

    def test_delay_step(self):
        s, _ = self.sess(); import time; t = time.time()
        s.run_step(new_step("delay", ms=150)); self.assertGreaterEqual(time.time() - t, 0.14)


class Helpers(unittest.TestCase):
    def test_match_expect(self):
        r = Reply(True, bytes.fromhex("50 03 00 32"))
        self.assertTrue(match_expect("50", r)[0]); self.assertTrue(match_expect("50 03", r)[0])
        self.assertFalse(match_expect("51", r)[0]); self.assertTrue(match_expect("50 ** 00", r)[0])
        self.assertFalse(match_expect("50 03 00 32 99", r)[0])
        self.assertEqual(default_expect(b"\x27\x01"), "67")

    def test_template(self):
        self.assertEqual(render_template("{addr} 00 {len}", {"addr": b"\x08\x00\x00\x00", "len": b"\x00\x01"}),
                         bytes.fromhex("08000000 00 0001".replace(" ", "")))
        with self.assertRaises(StepError): render_template("{nope}", {})

    def test_routine_multi_region_and_template(self):
        cfg = DiagConfig(); bus, ecu = make_sim(cfg.addr, "new", chatter=False)
        s = DiagSession(bus, cfg); self.addCleanup(lambda: (s.close(), ecu.stop()))
        s.last_download = ("x", [Segment(0x1000, b"AAAA"), Segment(0x2000, b"BBBBBB")])
        sent = []
        s.exchange = lambda pdu, addr="phys", suppress=False: (sent.append(pdu), Reply(True, bytes([0x71, pdu[1]]) + pdu[2:4] + b"\0"))[1]
        s.run_step(new_step("routine", sub="01", rid="01 02", use_crc=True, data="{addr} {len}", crc=PRESETS["CRC32"].to_dict()))
        self.assertEqual(len(sent), 2)
        self.assertEqual(sent[0], bytes.fromhex("3101 0102") + (0x1000).to_bytes(4, "big") + (4).to_bytes(4, "big") + zlib.crc32(b"AAAA").to_bytes(4, "big"))
        self.assertEqual(sent[1][-4:], zlib.crc32(b"BBBBBB").to_bytes(4, "big"))
        sent.clear()
        s.run_step(new_step("routine", sub="01", rid="DF FE", use_crc=True, per_region=False, crc=PRESETS["CRC16/XMODEM"].to_dict(), crc_order="little"))
        self.assertEqual(len(sent), 1); self.assertEqual(len(sent[0]), 4 + 2)

    def test_summary(self):
        self.assertEqual(step_summary(new_step("routine", sub="01", rid="DF FE", use_crc=True)), "31 01 DF FE [CRC32]")


def ihex(segments):
    """[(addr, bytes)] -> Intel HEX text (extended linear address records)."""
    def rec(typ, addr, data):
        body = bytes([len(data), addr >> 8 & 0xFF, addr & 0xFF, typ]) + data
        return ":" + (body + bytes([(-sum(body)) & 0xFF])).hex().upper()
    out = []
    for a, d in segments:
        out.append(rec(4, 0, (a >> 16).to_bytes(2, "big")))
        for off in range(0, len(d), 16):
            out.append(rec(0, (a + off) & 0xFFFF, d[off:off + 16]))
    out.append(rec(1, 0, b""))
    return "\n".join(out) + "\n"


SEG_A, SEG_B = (0x08000000, bytes(range(1, 33))), (0x08001000, bytes(range(100, 108)))


def reqs(events):
    return [e.data for e in events if e.kind == "REQ"]


def flash_run(product, mutate=None, segs=(SEG_A, SEG_B), **simkw):
    """Full sample plan with a two-segment HEX file; returns (ok, ecu, steps, events)."""
    cfg = DiagConfig(); cfg.settings.p2_ms = 1500
    bus, ecu = make_sim(cfg.addr, product, **simkw)
    d = tempfile.mkdtemp(); fw = os.path.join(d, "app.hex"); open(fw, "w").write(ihex(segs))
    plan = make_plan(product, fw, tmpfile(os.urandom(64), "drv.bin"))
    if mutate: mutate(plan)
    events, steps = [], {}
    h = Hooks(); h.log = events.append; h.step = lambda i, st, dd: steps.__setitem__(i, (st, dd))
    sess = DiagSession(bus, cfg, h)
    try: ok = sess.run_plan(plan.steps)
    finally: sess.close(); ecu.stop()
    return ok, ecu, steps, events


class EraseAndCrc(unittest.TestCase):
    def test_erase_pdu_matches_spec_table(self):
        ok, ecu, steps, ev = flash_run("old")
        self.assertTrue(ok, steps)
        er = [r for r in reqs(ev) if r[:4] == bytes.fromhex("31 01 DF FF")]
        self.assertEqual(len(er), 1)                                   # erase runs once in the whole flow
        # 31 01 DFFF | 44 | start | size (sum of both segments = 32 + 8) | ext 01
        self.assertEqual(er[0], bytes.fromhex("31 01 DF FF 44") + (0x08000000).to_bytes(4, "big") + (40).to_bytes(4, "big") + b"\x01")
        self.assertEqual(ecu.erased, [(0x08000000, 40, b"\x01")])
        i_erase = next(i for i, r in enumerate(reqs(ev)) if r[:2] == b"\x31\x01" and r[2:4] == b"\xDF\xFF")
        i_34 = next(i for i, r in enumerate(reqs(ev)) if r[0] == 0x34 and i > i_erase)
        self.assertLess(i_erase, i_34)                                 # erase before the app download

    def test_erase_extension_byte_editable_and_optional(self):
        def mut(val, on):
            def f(plan):
                for s in plan.steps:
                    if s.get("rkind") == "erase": s["ext_on"], s["ext"] = on, val
            return f
        ok, ecu, *_ = flash_run("new", mut("A5", True)); self.assertTrue(ok); self.assertEqual(ecu.erased[0][2], b"\xA5")
        ok, ecu, *_ = flash_run("new", mut("01", False)); self.assertTrue(ok); self.assertEqual(ecu.erased[0][2], b"")
        self.assertEqual(len(ecu.erase_requests[0]), 13)                                  # 31 sub rid(2) alfi addr(4) size(4), no ext
        ok, ecu, steps, _ = flash_run("new", mut("A5 B6", True)); self.assertFalse(ok); self.assertIn("1 字节", steps[6][1])

    def test_erase_size_sum_vs_span_and_lengths(self):
        def rng(mode, al=4, sl=4):
            def f(plan):
                for s in plan.steps:
                    if s.get("rkind") == "erase": s["erase_range"], s["addr_len"], s["size_len"] = mode, al, sl
            return f
        ok, ecu, _, ev = flash_run("new", rng("sum")); self.assertEqual(ecu.erased[0][:2], (0x08000000, 40))
        self.assertTrue(any("地址不连续" in e.text for e in ev))
        ok, ecu, _, ev = flash_run("new", rng("span")); self.assertEqual(ecu.erased[0][:2], (0x08000000, 0x1008))
        ok, ecu, *_ = flash_run("new", rng("sum", 4, 2))
        self.assertEqual(ecu.erase_requests[0][4], 0x24)                # format byte: high nibble = size length, low = address length
        self.assertEqual(ecu.erased[0][:2], (0x08000000, 40))
        ok, ecu, steps, _ = flash_run("new", rng("sum", 2, 4)); self.assertFalse(ok); self.assertIn("超出", steps[6][1])
        contiguous = (SEG_A, (0x08000020, bytes(16)))
        ok, ecu, _, ev = flash_run("new", rng("span"), segs=contiguous); self.assertEqual(ecu.erased[0][1], 48)
        self.assertFalse(any("地址不连续" in e.text for e in ev))

    def test_segment_crc_runs_after_each_segment_then_whole_file(self):
        ok, ecu, steps, ev = flash_run("new")
        self.assertTrue(ok, steps)
        seq = []
        for r in reqs(ev):
            if r[0] in (0x34, 0x37): seq.append(f"{r[0]:02X}")
            elif r[0] == 0x31 and r[2:4] == b"\x01\x02": seq.append("SEG")
            elif r[0] == 0x31 and r[2:4] == b"\x01\x03": seq.append("FILE")
        self.assertEqual(" ".join(seq[2:]), "34 37 SEG 34 37 SEG FILE")      # (driver 34 37 first) then per-segment checks
        crcs = [r for r in reqs(ev) if r[0] == 0x31 and r[2:4] in (b"\x01\x02", b"\x01\x03")]
        self.assertEqual([len(r) for r in crcs], [8, 8, 8])                 # 31 sub rid + 4-byte CRC only
        self.assertEqual(crcs[0][4:], zlib.crc32(SEG_A[1]).to_bytes(4, "big"))
        self.assertEqual(crcs[1][4:], zlib.crc32(SEG_B[1]).to_bytes(4, "big"))
        self.assertEqual(crcs[2][4:], zlib.crc32(SEG_A[1] + SEG_B[1]).to_bytes(4, "big"))
        self.assertEqual(ecu.crc_checked, [(0x0102, True), (0x0102, True), (0x0103, True)])
        self.assertIn("分段CRC", steps[7][1]); self.assertIn("全文件", steps[8][1])

    def test_whole_file_mode_checks_once_after_download(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["crc_mode"] = "file"
        ok, ecu, steps, ev = flash_run("old", mut)
        self.assertTrue(ok, steps)
        self.assertEqual(ecu.crc_checked, [(0xDFFD, True)])
        last34 = max(i for i, r in enumerate(reqs(ev)) if r[0] == 0x34)
        crc_at = [i for i, r in enumerate(reqs(ev)) if r[:4] == bytes.fromhex("31 01 DF FD")]
        self.assertEqual(len(crc_at), 1); self.assertGreater(crc_at[0], last34)
        self.assertNotIn("分段", steps[7][1])

    def test_user_defined_identifiers_and_subfunction(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["rid"], s["rid_file"], s["sub"] = "AB CD", "12 34", "03"
                if s.get("rkind") == "erase": s["rid"], s["sub"] = "55 66", "02"
        ok, ecu, steps, ev = flash_run("new", mut, erase_rids=(0x5566,), seg_crc_rids=(0xABCD,), file_crc_rids=(0x1234,))
        self.assertTrue(ok, steps)
        self.assertTrue(any(r[:4] == bytes.fromhex("31 02 55 66") for r in reqs(ev)))
        self.assertTrue(any(r[:4] == bytes.fromhex("31 03 AB CD") for r in reqs(ev)))
        self.assertTrue(any(r[:4] == bytes.fromhex("31 03 12 34") for r in reqs(ev)))

    def test_empty_file_rid_falls_back_to_segment_rid(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["rid_file"] = ""
        ok, ecu, *_ = flash_run("new", mut, file_crc_rids=(0x0102,))
        self.assertEqual(len(ecu.crc_checked), 3)

    def test_crc_step_alone_does_both_when_not_interleaved(self):
        cfg = DiagConfig(); bus, ecu = make_sim(cfg.addr, "new", chatter=False)
        s = DiagSession(bus, cfg); self.addCleanup(lambda: (s.close(), ecu.stop()))
        s.last_download = ("x", [Segment(0x1000, b"AAAA"), Segment(0x2000, b"BBBBBB")])
        sent = []
        s.exchange = lambda pdu, addr="phys", suppress=False: (sent.append(pdu), Reply(True, bytes([0x71, pdu[1]]) + pdu[2:4] + b"\0"))[1]
        d = s.run_step(new_step("routine", rkind="crc", sub="01", rid="01 02", rid_file="01 03", crc_mode="region"))
        self.assertEqual([p[2:4].hex() for p in sent], ["0102", "0102", "0103"]); self.assertIn("全文件", d)
        self.assertEqual(sent[2][4:], zlib.crc32(b"AAAABBBBBB").to_bytes(4, "big"))

    def test_crc_byte_order_and_type_apply_to_both(self):
        def mut(plan):
            for s in plan.steps:
                if s.get("rkind") == "crc": s["crc"] = PRESETS["CRC16/XMODEM"].to_dict(); s["crc_order"] = "little"
        ok, ecu, steps, ev = flash_run("new", mut)
        crcs = [r for r in reqs(ev) if r[0] == 0x31 and r[2:4] in (b"\x01\x02", b"\x01\x03")]
        self.assertFalse(ok)                                                # the sim checks CRC32, so the ECU rejects it
        self.assertIn("分段CRC", steps[7][1])
        spec = CrcSpec.from_dict(PRESETS["CRC16/XMODEM"].to_dict())
        self.assertEqual(crcs[0][4:], crc_bytes(spec, SEG_A[1], "little")); self.assertEqual(len(crcs[0]), 6)

    def test_summaries_and_preflight(self):
        from zdiag.engine import preflight
        e = new_step("routine", rkind="erase", sub="01", rid="DF FF")
        self.assertEqual(step_summary(e), "31 01 DF FF 44 [起始地址][大小] 01")
        e["ext_on"] = False; self.assertEqual(step_summary(e), "31 01 DF FF 44 [起始地址][大小]")
        c = new_step("routine", rkind="crc", sub="01", rid="DF FE", rid_file="DF FD")
        self.assertEqual(step_summary(c), "分段 31 01 DF FE + 全文件 31 01 DF FD [CRC32]")
        c["crc_mode"] = "file"; self.assertEqual(step_summary(c), "全文件 31 01 DF FD [CRC32]")
        f = tmpfile(b"x" * 8)
        self.assertEqual(preflight([new_step("download", file=f), e, c]), [])
        bad = preflight([new_step("routine", rkind="erase", rid="DF"), new_step("routine", rkind="crc", rid="DF FE", rid_file="1"),
                         new_step("routine", rkind="erase", rid="DF FF", ext="01 02")])
        self.assertEqual(len(bad), 5, bad)        # step1: id + no download; step2: bad file id; step3: ext + no download
        self.assertEqual(len(preflight([e])), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class Preflight(unittest.TestCase):
    def test_reports_all_problems(self):
        from zdiag.engine import preflight
        ok_file = tmpfile(b"x" * 10)
        steps = [new_step("service", pdu=""), new_step("unlock", dll=""), new_step("download", file="/nonexistent.bin"),
                 new_step("routine", rid="DF", use_crc=True), new_step("driver", file=ok_file)]
        probs = preflight(steps)
        self.assertEqual(len(probs), 5, probs)
        good = [new_step("service", pdu="10 03"), new_step("unlock", dll="builtin:xor"),
                new_step("download", file=ok_file), new_step("routine", rid="DF FE", use_crc=True)]
        self.assertEqual(preflight(good), [])
        self.assertEqual(len(preflight([new_step("routine", rid="DF FE", use_crc=True)])), 1)
