import os, sys, tempfile, threading, unittest, zlib
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.crc import PRESETS, CrcSpec
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
    def check_full(self, product, rid, seed_len):
        ok, ecu, steps, ev, fw, drv = run(product)
        self.assertTrue(ok, steps)
        self.assertEqual(ecu.seed_len, seed_len)
        self.assertEqual(len(ecu.seed), seed_len)              # seed length as the ECU generation dictates
        self.assertEqual(ecu.crc_checked, [(rid, True)])
        self.assertEqual(ecu.reset_count, 1)
        self.assertTrue(all(v[0] == "ok" for v in steps.values()) and len(steps) == 9)
        self.assertEqual(bytes(ecu.mem[0 + i] for i in range(len(fw))), fw)   # last 34 target addr 0

    def test_new_product_16byte_seed_and_0102(self): self.check_full("new", 0x0102, 16)
    def test_old_product_2byte_seed_and_DFFE(self):   self.check_full("old", 0xDFFE, 2)

    def test_wrong_crc_algorithm_stops_before_reset(self):
        def mut(plan):
            for s in plan.steps:
                if s["type"] == "routine": s["crc"] = PRESETS["CRC32/MPEG-2"].to_dict()
        ok, ecu, steps, ev, *_ = run("new", mut)
        self.assertFalse(ok)
        self.assertEqual(steps[7][0], "fail"); self.assertIn("0x72", steps[7][1])
        self.assertEqual(ecu.reset_count, 0); self.assertNotIn(8, steps)

    def test_wrong_rid_for_product_fails(self):
        def mut(plan):
            for s in plan.steps:
                if s["type"] == "routine": s["rid"] = "DF FE"          # new ECU does not know it
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
        self.assertEqual(steps[6], ("fail", "已取消")); self.assertEqual(ecu.reset_count, 0)

    def test_repeat_runs(self):
        cfg = DiagConfig(); bus, ecu = make_sim(cfg.addr, "old")
        plan = make_plan("old", tmpfile(os.urandom(500)), tmpfile(os.urandom(100), "d.bin"))
        sess = DiagSession(bus, cfg)
        try: self.assertTrue(sess.run_plan(plan.steps, repeat=2, interval_s=0.05))
        finally: sess.close(); ecu.stop()
        self.assertEqual(ecu.reset_count, 2); self.assertEqual(len(ecu.crc_checked), 2)

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
