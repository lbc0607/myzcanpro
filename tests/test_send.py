import os, sys, tempfile, threading, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag.bus import CanFrame
from zdiag.sendlist import PARALLEL, SERIAL, SendItem, build_schedule, frame_at, load_list, run_send, save_list


class FakeBus:
    def __init__(self, clock, fail_on=()):
        self.clock, self.sent, self.fail_on = clock, [], set(fail_on)
    def send(self, fr):
        if len(self.sent) in self.fail_on:
            self.fail_on.discard(len(self.sent)); self.sent.append(None); raise IOError("tx full")
        self.sent.append((round(self.clock.t, 3), fr))


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def sleep(self, s): self.t += s


def run(items, bus=None, clk=None, **kw):
    clk = clk or Clock(); bus = bus or FakeBus(clk)
    status = []
    res = run_send(bus, items, clock=clk, sleep=clk.sleep, on_status=lambda i, s: status.append((i, s)), **kw)
    return res, bus, status


class FrameTests(unittest.TestCase):
    def test_padding_to_length_and_increments(self):
        it = SendItem(can_id=0x7E2, data=bytes([2, 0x10, 1]), length=8)
        self.assertEqual(frame_at(it, 0).data, bytes([2, 0x10, 1, 0, 0, 0, 0, 0]))
        it.id_inc = it.data_inc = True
        f = frame_at(it, 3)
        self.assertEqual((f.can_id, f.data), (0x7E5, bytes([2, 0x10, 1, 0, 0, 0, 0, 3])))
        it.data = bytes([0, 0, 0, 0, 0, 0, 0, 0xFF]); self.assertEqual(frame_at(it, 1).data, bytes([0, 0, 0, 0, 0, 0, 1, 0]))   # carry
        it.data = bytes([0xFF] * 8); self.assertEqual(frame_at(it, 1).data, bytes(8))                                       # wraps
        it2 = SendItem(can_id=0x7FF, id_inc=True); self.assertEqual(frame_at(it2, 1).can_id, 0)                           # id wraps
        it3 = SendItem(can_id=0x1FFFFFFF, extended=True, id_inc=True); self.assertEqual(frame_at(it3, 1).can_id, 0)

    def test_remote_and_fd(self):
        r = frame_at(SendItem(remote=True, length=4), 0)
        self.assertTrue(r.rtr); self.assertEqual(len(r.data), 4)
        f = frame_at(SendItem(length=12, data=b"\x01"), 0, fd=True, brs=True)
        self.assertTrue(f.fd and f.brs); self.assertEqual(len(f.data), 12)
        self.assertFalse(frame_at(SendItem(), 0, fd=False, brs=True).brs)

    def test_validation(self):
        for bad, fd in ((SendItem(can_id=0x800), False), (SendItem(length=9), False), (SendItem(length=9), True),
                        (SendItem(length=2, data=b"123"), False), (SendItem(remote=True), True),
                        (SendItem(per_send=0), False), (SendItem(count=0), False), (SendItem(interval_ms=-1), False),
                        (SendItem(remote=True, length=9), False)):
            with self.assertRaises(ValueError, msg=repr(bad)):
                bad.validate(fd)
        SendItem(length=64).validate(True); SendItem(can_id=0x1FFFFFFF, extended=True).validate()


class ScheduleTests(unittest.TestCase):
    def test_per_send_rounds_and_interval(self):
        res, bus, _ = run([SendItem(count=3, per_send=2, interval_ms=100, id_inc=True, can_id=0x100)])
        self.assertEqual([(t, f.can_id) for t, f in bus.sent],
                         [(0, 0x100), (0, 0x101), (0.1, 0x102), (0.1, 0x103), (0.2, 0x104), (0.2, 0x105)])
        self.assertEqual((res.sent, res.failed, res.ok), (6, 0, True)); self.assertAlmostEqual(res.elapsed, 0.2, 2)

    def test_parallel_interleaves_serial_does_not(self):
        a = SendItem(can_id=1, count=3, interval_ms=100); b = SendItem(can_id=2, count=2, interval_ms=150)
        _, bus, _ = run([a, b], mode=PARALLEL)
        self.assertEqual([(t, f.can_id) for t, f in bus.sent], [(0, 1), (0, 2), (0.1, 1), (0.15, 2), (0.2, 1)])
        _, bus, _ = run([a, b], mode=SERIAL)
        self.assertEqual([(t, f.can_id) for t, f in bus.sent], [(0, 1), (0.1, 1), (0.2, 1), (0.2, 2), (0.35, 2)])

    def test_speed_multiplier(self):
        _, bus, _ = run([SendItem(count=3, interval_ms=100)], speed=2)
        self.assertEqual([t for t, _ in bus.sent], [0, 0.05, 0.1])
        with self.assertRaises(ValueError):
            build_schedule([SendItem()], speed=0)

    def test_status_callbacks(self):
        _, _, st = run([SendItem(can_id=1, count=2, interval_ms=10), SendItem(can_id=2)])
        self.assertEqual([s for i, s in st if i == 0], ["发送中", "完成"]); self.assertEqual([s for i, s in st if i == 1], ["发送中", "完成"])

    def test_stop_on_fail_vs_continue(self):
        items = [SendItem(can_id=1, count=4, interval_ms=10)]
        clk = Clock(); res, bus, st = run(items, FakeBus(clk, fail_on=[1]), clk, stop_on_fail=True)
        self.assertEqual((res.sent, res.failed, res.stopped_on_fail, res.ok), (1, 1, True, False)); self.assertEqual(st[-1], (0, "失败"))
        clk = Clock(); res, bus, st = run(items, FakeBus(clk, fail_on=[1]), clk, stop_on_fail=False)
        self.assertEqual((res.sent, res.failed, res.stopped_on_fail), (3, 1, False)); self.assertEqual(st[-1], (0, "失败"))

    def test_cancel_midway(self):
        clk, ev = Clock(), threading.Event()
        bus = FakeBus(clk)
        orig = bus.send
        bus.send = lambda fr: (orig(fr), ev.set() if len(bus.sent) == 2 else None)[0]
        res, _, st = run([SendItem(count=10, interval_ms=100)], bus, clk, cancel=ev)
        self.assertEqual(res.sent, 2); self.assertTrue(res.cancelled); self.assertEqual(st[-1], (0, "已停止"))

    def test_invalid_item_blocks_whole_run(self):
        clk = Clock(); bus = FakeBus(clk)
        with self.assertRaises(ValueError):
            run_send(bus, [SendItem(), SendItem(length=9)], clock=clk, sleep=clk.sleep)
        self.assertEqual(bus.sent, [])


class FileTests(unittest.TestCase):
    def test_roundtrip(self):
        items = [SendItem(can_id=0x7E2, data=bytes([2, 0x10, 1]), name="进入会话", count=3, interval_ms=20, id_inc=True),
                 SendItem(can_id=0x18DAF110, extended=True, remote=True, length=2, checked=False)]
        p = os.path.join(tempfile.mkdtemp(), "list.json"); save_list(p, items)
        back = load_list(p)
        self.assertEqual(back, items)
        open(p, "w", encoding="utf-8").write('{"format": "x"}')
        with self.assertRaises(ValueError):
            load_list(p)


if __name__ == "__main__":
    unittest.main()
