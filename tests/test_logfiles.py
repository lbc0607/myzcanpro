import os, sys, tempfile, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import CanFrame
from zdiag.logfiles import part_path, purge_old
from zdiag.monitor import FrameTap, CsvRecorder
from zdiag.project import LogCfg
from zdiag_ui.logsaver import LogSaver


class LogFiles(unittest.TestCase):
    def setUp(self): self.d = tempfile.mkdtemp()

    def test_part_path(self):
        self.assertEqual(part_path("a/b.txt", 0), "a/b.txt"); self.assertEqual(part_path("a/b.txt", 2), "a/b_2.txt")

    def test_saver_rotates_by_size_and_lines(self):
        s = LogSaver(); base = os.path.join(self.d, "uds_x.txt")
        s.start(base, max_lines=1000, max_bytes=100)
        for i in range(30): s.write("0123456789")                 # 11 bytes each -> 9 per file
        s.stop()
        sizes = [os.path.getsize(part_path(base, i)) for i in range(4)]
        self.assertTrue(all(0 < n <= 100 for n in sizes), sizes); self.assertTrue(os.path.exists(part_path(base, 3)))
        s2 = LogSaver(); b2 = os.path.join(self.d, "uds_y.txt"); s2.start(b2, max_lines=5)
        for i in range(12): s2.write("x")
        s2.stop(); self.assertEqual([len(open(part_path(b2, i)).read().split()) for i in range(3)], [5, 5, 2])

    def test_one_line_larger_than_limit_is_not_lost_or_looping(self):
        s = LogSaver(); base = os.path.join(self.d, "uds_z.txt"); s.start(base, max_bytes=10)
        for _ in range(3): s.write("y" * 50)
        s.stop(); self.assertEqual(sum(len(open(part_path(base, i)).read().split()) for i in range(3)), 3)

    def test_purge_only_old_auto_named_files(self):
        now = time.time()
        def mk(name, age_days):
            p = os.path.join(self.d, name); open(p, "w").close(); t = now - age_days * 86400; os.utime(p, (t, t)); return p
        old, new, other, odd = mk("uds_old.txt", 5), mk("flash_new.txt", 1), mk("notes.txt", 50), mk("monitor_o.csv", 9)
        self.assertEqual(purge_old(self.d, 0), 0); self.assertTrue(os.path.exists(old))          # 0 = keep forever
        self.assertEqual(purge_old(self.d, 2), 2)
        self.assertFalse(os.path.exists(old) or os.path.exists(odd)); self.assertTrue(os.path.exists(new) and os.path.exists(other))
        self.assertEqual(purge_old(os.path.join(self.d, "nope"), 2), 0)

    def test_csv_recorder_rotates_with_header(self):
        base = os.path.join(self.d, "monitor_r.csv")
        t = FrameTap(); t.start_record(base, max_bytes=2000)
        for i in range(200): t.feed(0, CanFrame(0x100 + (i % 5), bytes(8)), 0)
        rec = t.stop_record()
        self.assertGreater(len(rec.paths), 1)
        for p in rec.paths:
            with open(p, encoding="utf-8-sig") as f: self.assertTrue(f.readline().startswith("序号"))
        self.assertEqual(sum(len(open(p, encoding="utf-8-sig").read().splitlines()) - 1 for p in rec.paths), 200)

    def test_logcfg_validation(self):
        LogCfg(max_file_mb=1, keep_days=0).validate(); LogCfg(max_file_mb=1024, keep_days=3650).validate()
        for bad in (LogCfg(max_file_mb=0), LogCfg(max_file_mb=10**9), LogCfg(keep_days=-1), LogCfg(keep_days=10**6)):
            with self.assertRaises(ValueError): bad.validate()


if __name__ == "__main__":
    unittest.main(verbosity=2)
