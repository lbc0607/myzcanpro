"""CAN file transfer (zdiag/cft.py) on in-memory cables: one / two links, loss, link cut + recovery, resume, folders, safety."""
import os, random, shutil, sys, tempfile, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import Bus, VirtualBus
from zdiag.cft import (M_REPLY, M_RESULT, T_MSG, Messenger, CftConfig, CftError, Link, Receiver, Sender, collect_entries, file_crc, safe_rel, PART, META)


class _End(Bus):
    def __init__(self, inner, cable, seed):
        self.inner, self.cable, self.rnd = inner, cable, random.Random(seed)
        self.sent = 0

    def send(self, fr):
        c = self.cable
        if c.cut or (c.drop and self.rnd.random() < c.drop) or (c.cut_dir and self.cut_dir_hit(fr)):
            return
        self.sent += 1
        self.inner.send(fr)

    def cut_dir_hit(self, fr):
        return False

    def recv(self, timeout):
        if self.cable.cut:
            time.sleep(min(timeout, 0.005))
            self.inner.flush()
            return None
        return self.inner.recv(timeout)

    def flush(self):
        self.inner.flush()


class Cable:
    """A CAN cable between the sender end and the receiver end; `cut` makes it silent, `drop` loses frames at random."""
    def __init__(self, seed=1, drop=0.0):
        a, b = VirtualBus.pair()
        self.cut, self.drop, self.cut_dir = False, drop, False
        self.s, self.r = _End(a, self, seed), _End(b, self, seed + 100)


def cfg(**kw):
    c = CftConfig(link_timeout=0.3, hb_ms=30, status_ms=10, rto_min=0.15, idle_timeout=8.0, chunk_bytes=1000)
    for k, v in kw.items():
        setattr(c, k, v)
    return c


def links(cables, side, c, fd=True, tag=""):
    return [Link(i, getattr(cb, side), fd, True, c, f"{tag}{i}") for i, cb in enumerate(cables)]


def make_file(path, n, seed=7):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    r = random.Random(seed)
    with open(path, "wb") as f:
        f.write(bytes(r.getrandbits(8) for _ in range(min(n, 4096))) * (n // 4096 + 1) if n else b"")
        f.truncate(n)


def same(a, b):
    return os.path.getsize(a) == os.path.getsize(b) and file_crc(a) == file_crc(b)


class Base(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.d, True)
        self.src, self.dst = os.path.join(self.d, "src"), os.path.join(self.d, "dst")

    def run_pair(self, cables, paths, c=None, fd=True, rx_cfg=None, wait=60, hook=None):
        c = c or cfg()
        snd = Sender(links(cables, "s", c, fd), c)
        rc = rx_cfg or cfg()
        rcv = Receiver(links(cables, "r", rc, fd), self.dst, rc)
        res = {}
        tr = threading.Thread(target=lambda: res.update(r=rcv.run()), daemon=True); tr.start()
        ts = threading.Thread(target=lambda: res.update(s=snd.run(paths)), daemon=True); ts.start()
        if hook:
            hook(snd, rcv)
        ts.join(wait)
        if ts.is_alive():
            snd.stop()
        tr.join(10)
        if tr.is_alive():
            rcv.stop(); tr.join(5)
        return snd, rcv, res


class Basic(Base):
    def test_single_link_fd_file(self):
        f = os.path.join(self.src, "a.bin"); make_file(f, 50_000)
        snd, rcv, res = self.run_pair([Cable()], [f])
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "a.bin")))
        self.assertEqual(int(os.stat(f).st_mtime * 1000), int(os.stat(os.path.join(self.dst, "a.bin")).st_mtime_ns // 1_000_000))
        self.assertEqual(sorted(os.listdir(self.dst)), ["a.bin"])                       # no .cftpart / .cftmeta left
        self.assertEqual((res["s"]["files_done"], res["s"]["bytes_done"]), (1, 50_000))

    def test_classic_can_8_byte_frames(self):
        f = os.path.join(self.src, "c.dat"); make_file(f, 6_000)
        snd, rcv, res = self.run_pair([Cable()], [f], fd=False)
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "c.dat")))
        self.assertEqual(snd.window_eff(), 16)                                          # classic status frame reports 16 chunks

    def test_empty_file_odd_sizes_and_any_content(self):
        files = {"empty.txt": 0, "one.bin": 1, "exact.bin": 2000, "odd.bin": 2001, "中文 名字.dat": 3333}
        paths = []
        for n, s in files.items():
            p = os.path.join(self.src, n); make_file(p, s, seed=s + 1); paths.append(p)
        snd, rcv, res = self.run_pair([Cable()], paths)
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        for n in files:
            self.assertTrue(same(os.path.join(self.src, n), os.path.join(self.dst, n)), n)

    def test_folder_tree_with_empty_dirs(self):
        top = os.path.join(self.src, "proj")
        for rel, n in (("a.txt", 10), ("sub/b.bin", 5000), ("sub/deep/c.bin", 12_345), ("sub/deep/d.txt", 0)):
            make_file(os.path.join(top, *rel.split("/")), n, seed=n + 3)
        os.makedirs(os.path.join(top, "emptydir"))
        snd, rcv, res = self.run_pair([Cable(), Cable()], [top])
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertEqual(res["s"]["files_done"], 4)
        for rel in ("a.txt", "sub/b.bin", "sub/deep/c.bin", "sub/deep/d.txt"):
            self.assertTrue(same(os.path.join(top, *rel.split("/")), os.path.join(self.dst, "proj", *rel.split("/"))), rel)
        self.assertTrue(os.path.isdir(os.path.join(self.dst, "proj", "emptydir")))


class Loss(Base):
    def test_random_frame_loss_is_repaired(self):
        f = os.path.join(self.src, "l.bin"); make_file(f, 60_000)
        snd, rcv, res = self.run_pair([Cable(drop=0.02)], [f])
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "l.bin")))

    def test_two_links_share_the_load(self):
        f = os.path.join(self.src, "t.bin"); make_file(f, 400_000)
        cs = [Cable(1), Cable(2)]
        snd, rcv, res = self.run_pair(cs, [f])
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "t.bin")))
        a, b = (l.tx_bytes for l in snd.links)
        self.assertGreater(min(a, b), 0.01 * (a + b))                                   # both links carried data (the split follows thread speed)

    def test_link_cut_midway_falls_back_to_one_link_and_recovers(self):
        f = os.path.join(self.src, "k.bin"); make_file(f, 1_500_000)
        cs = [Cable(1), Cable(2)]
        events = {}

        def hook(snd, rcv):
            t0 = time.time()
            while time.time() - t0 < 20 and snd.status()["bytes_done"] < 300_000:
                time.sleep(0.01)
            cs[1].cut = True; events["cut"] = time.time()
            while time.time() - t0 < 30 and snd.links[1].alive:
                time.sleep(0.005)
            events["detected"] = time.time(); events["down_at_bytes"] = snd.status()["bytes_done"]
            time.sleep(0.5)
            events["bytes_single"] = snd.status()["bytes_done"]
            cs[1].cut = False
            time.sleep(0.01)

        snd, rcv, res = self.run_pair(cs, [f], hook=hook)
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "k.bin")))
        self.assertLess(events["detected"] - events["cut"], 1.0)                        # noticed within ~link_timeout
        self.assertGreater(events["bytes_single"], events["down_at_bytes"])             # the other link kept going
        self.assertGreaterEqual(snd.links[1].downs, 1)

    def test_start_with_one_dead_link(self):
        f = os.path.join(self.src, "d.bin"); make_file(f, 100_000)
        cs = [Cable(1), Cable(2)]; cs[1].cut = True
        snd, rcv, res = self.run_pair(cs, [f])
        self.assertEqual(res["s"]["state"], "done", res["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "d.bin")))
        self.assertFalse(snd.links[1].alive)

    def test_all_links_down_gives_up_with_clear_error(self):
        f = os.path.join(self.src, "x.bin"); make_file(f, 2_000_000)
        cs = [Cable(1)]
        c = cfg(idle_timeout=1.0)

        def hook(snd, rcv):
            while snd.status()["bytes_done"] < 50_000:
                time.sleep(0.01)
            cs[0].cut = True
        snd, rcv, res = self.run_pair(cs, [f], c=c, hook=hook, wait=30)
        self.assertEqual(res["s"]["state"], "error"); self.assertIn("断开", res["s"]["error"])


class Resume(Base):
    def test_resume_after_abort_continues_not_restarts(self):
        f = os.path.join(self.src, "r.bin"); make_file(f, 600_000)
        cs = [Cable()]

        def hook(snd, rcv):
            while snd.status()["bytes_done"] < 200_000:
                time.sleep(0.005)
            snd.stop()
        snd, rcv, res = self.run_pair(cs, [f], hook=hook, wait=20)
        self.assertNotEqual(res["s"]["state"], "done")
        part = os.path.join(self.dst, "r.bin" + PART)
        self.assertTrue(os.path.isfile(part) and os.path.isfile(os.path.join(self.dst, "r.bin" + META)))
        # second run: receives only what is missing
        cs2 = [Cable()]
        snd2, rcv2, res2 = self.run_pair(cs2, [f])
        self.assertEqual(res2["s"]["state"], "done", res2["s"]["error"])
        self.assertTrue(same(f, os.path.join(self.dst, "r.bin")))
        self.assertLess(snd2.links[0].tx_bytes, 600_000 * 1.2 * 0.85)                    # clearly less than a full re-send
        self.assertTrue(any("续传" in l for l in snd2.log.lines))

    def test_changed_source_restarts_and_identical_file_is_skipped(self):
        f = os.path.join(self.src, "s.bin"); make_file(f, 30_000)
        snd, rcv, res = self.run_pair([Cable()], [f])
        self.assertEqual(res["s"]["state"], "done")
        snd, rcv, res = self.run_pair([Cable()], [f])                                    # same file again: skipped
        self.assertEqual((res["s"]["files_skipped"], res["s"]["files_done"]), (1, 0))
        self.assertTrue(any(c.tx_frames for c in [snd.links[0]]) and snd.links[0].tx_bytes < 2000)
        make_file(f, 30_000, seed=99)                                                    # different content -> replaced
        snd, rcv, res = self.run_pair([Cable()], [f])
        self.assertEqual(res["s"]["files_done"], 1)
        self.assertTrue(same(f, os.path.join(self.dst, "s.bin")))

    def test_no_overwrite_option(self):
        f = os.path.join(self.src, "o.bin"); make_file(f, 5_000)
        os.makedirs(self.dst)
        with open(os.path.join(self.dst, "o.bin"), "wb") as fh: fh.write(b"mine")
        snd, rcv, res = self.run_pair([Cable()], [f], rx_cfg=cfg(overwrite=False))
        with open(os.path.join(self.dst, "o.bin"), "rb") as fh: self.assertEqual(fh.read(), b"mine")
        self.assertEqual(res["s"]["files_skipped"], 1)


class MessengerDup(Base):
    def test_late_copy_over_the_other_link_is_not_delivered_again(self):
        """A reply is sent on both links; the slower copy may arrive after the NEXT message and must not be taken for a new reply."""
        c = cfg()
        la = [Link(0, Cable().r, True, True, c), Link(1, Cable().r, True, True, c)]
        m = Messenger(la, 0x6A1, threading.Event(), lambda l: True)

        def frame(mid, kind, pay):
            body = bytes([kind]) + pay
            return bytes([T_MSG, mid, 0x80, len(body)]) + body
        m.on_msg(la[0], frame(5, M_REPLY, b"a")); m.on_msg(la[0], frame(6, M_RESULT, b"b")); m.on_msg(la[1], frame(5, M_REPLY, b"a"))
        got = []
        while not m.inbox.empty():
            got.append(m.inbox.get()[0])
        self.assertEqual(got, [M_REPLY, M_RESULT])


class Safety(Base):
    def test_unsafe_paths_are_refused(self):
        for bad in ("../x", "a/../../x", "/abs", "C:/x", "a\\b", "a//b", "f" + PART, "x/" + META):
            with self.assertRaises(CftError, msg=bad): safe_rel(bad)
        self.assertEqual(safe_rel("a/b c/中文.txt"), ["a", "b c", "中文.txt"])

    def test_bad_arguments(self):
        c = Cable()
        with self.assertRaises(CftError): Sender([], cfg())
        with self.assertRaises(CftError): Sender(links([c], "s", cfg(), fd=False), cfg(chunk_bytes=1400))   # >256 frames per chunk
        with self.assertRaises(CftError): cfg(window=500).validate()
        with self.assertRaises(CftError): collect_entries([os.path.join(self.d, "nope")])

    def test_collect_order_and_symlink_skip(self):
        top = os.path.join(self.src, "t")
        make_file(os.path.join(top, "b.txt"), 3); make_file(os.path.join(top, "a", "z.txt"), 4)
        try:
            os.symlink(os.path.join(top, "b.txt"), os.path.join(top, "link"))
        except OSError:
            pass
        rels = [e["rel"] for e in collect_entries([top])]
        self.assertEqual(rels, ["t", "t/a", "t/b.txt", "t/a/z.txt"])      # a folder: its own entries first, then sub-folders


if __name__ == "__main__":
    unittest.main()


class ErrorStormPacing(unittest.TestCase):
    def test_link_backs_off_on_error_burst_and_recovers(self):
        from zdiag.cft import CftConfig, Link

        class St: err = 0
        class B:
            stats = St()
            def send(self, fr): pass
        b = B()
        l = Link(0, b, False, False, CftConfig())
        self.assertFalse(l.pace())
        b.stats.err += 500; self.assertTrue(l.pace()); self.assertGreater(l.backoff, 0)
        first = l.backoff
        b.stats.err += 500; l.pace(); self.assertGreater(l.backoff, first)             # keeps slowing down while it lasts
        self.assertLessEqual(l.backoff, 0.25)
        for _ in range(12): l.pace()                                                    # calm again -> full speed
        self.assertEqual(l.backoff, 0.0)

    def test_link_info_carries_bus_health(self):
        from zdiag.cft import CftConfig, Link, Sender

        class St: err = 7
        class B:
            stats = St()
            def health(self): return {"tec": 130, "rec": 4, "bus_off": False, "recoveries": 2}
        l = Link(0, B(), False, False, CftConfig())
        info = Sender._link_info(l)
        self.assertEqual((info["bus_errors"], info["health"]["tec"]), (7, 130))
        self.assertIn("TEC=130", Sender._diag_text(l)); self.assertIn("重启通道 2 次", Sender._diag_text(l))
