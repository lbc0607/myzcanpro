"""Virtual multi-process CAN bus (zdiag/netbus.py): in-process semantics + two real Python processes moving a file over it."""
import os, random, shutil, socket, subprocess, sys, tempfile, textwrap, threading, time, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag import netbus
from zdiag.bus import CanFrame
from zdiag.devmgr import ChannelCfg, DeviceManager
from zdiag.project import NET_DEVICE


def free_port():
    s = socket.socket(); s.bind(("127.0.0.1", 0)); p = s.getsockname()[1]; s.close(); return p


class Semantics(unittest.TestCase):
    def setUp(self):
        self.port = free_port()
        os.environ["CANVDS_NETBUS_PORT"] = str(self.port)
        self.hub = netbus.Hub(self.port)
        self.addCleanup(self.hub.stop)
        self.addCleanup(os.environ.pop, "CANVDS_NETBUS_PORT", None)

    def test_frames_go_to_the_others_on_the_same_bus_only(self):
        cfg = ChannelCfg()
        a, b, c, other = (netbus.NetBus(0, cfg), netbus.NetBus(0, cfg), netbus.NetBus(0, cfg), netbus.NetBus(1, cfg))
        for x in (a, b, c, other):
            self.addCleanup(x.close)
        time.sleep(0.2)
        a.send(CanFrame(0x123, b"\x01\x02\x03", True, False, False))
        a.send(CanFrame(0x7E0, bytes(range(64)), False, True, True))
        for x in (b, c):
            f = x.recv(1.0); g = x.recv(1.0)
            self.assertEqual((f.can_id, f.data, f.extended, f.fd), (0x123, b"\x01\x02\x03", True, False))
            self.assertEqual((g.can_id, len(g.data), g.fd, g.brs), (0x7E0, 64, True, True))
        self.assertIsNone(a.recv(0.2))                                 # no echo to the sender
        self.assertIsNone(other.recv(0.2))                             # another bus number: separate bus
        self.assertEqual((a.stats.tx, b.stats.rx), (2, 2))

    def test_remote_frame_and_peer_leaving(self):
        cfg = ChannelCfg()
        a, b = netbus.NetBus(0, cfg), netbus.NetBus(0, cfg)
        time.sleep(0.1)
        a.send(CanFrame(0x55, b"\x00\x00", rtr=True))
        self.assertTrue(b.recv(1.0).rtr)
        b.close(); time.sleep(0.2)
        a.send(CanFrame(1, b"x"))                                      # hub drops the gone peer, the sender is unaffected
        a.close()

    def test_device_manager_integration_and_tap(self):
        dm = DeviceManager(tempfile.gettempdir())
        dm.open(NET_DEVICE); self.addCleanup(dm.shutdown)
        dm.start_channel(0, ChannelCfg(protocol="CAN FD")); dm.start_channel(1, ChannelCfg(protocol="CAN FD"))
        peer = netbus.NetBus(0, ChannelCfg())
        self.addCleanup(peer.close)
        time.sleep(0.2)
        bus = dm.acquire(0, self)
        peer.send(CanFrame(0x321, b"hi"))
        self.assertEqual(bus.recv(1.0).can_id, 0x321)
        bus.send(CanFrame(0x322, b"yo"))
        self.assertEqual(peer.recv(1.0).can_id, 0x322)
        self.assertEqual(dm.stats_of(0).snapshot()[:2], (1, 1))
        dm.release(self)


CHILD = textwrap.dedent('''
    import sys, os, time
    sys.path.insert(0, {root!r})
    from zdiag.devmgr import ChannelCfg, DeviceManager
    from zdiag.project import NET_DEVICE
    from zdiag.cft import CftConfig, Link, Receiver, Sender
    role, path, chans = sys.argv[1], sys.argv[2], [int(x) for x in sys.argv[3].split(",")]
    dm = DeviceManager(os.getcwd()); dm.open(NET_DEVICE)
    c = CftConfig(link_timeout=0.5, hb_ms=40, status_ms=15, idle_timeout=20.0)
    links = []
    for i, ch in enumerate(chans):
        dm.start_channel(ch, ChannelCfg(protocol="CAN FD"))
        links.append(Link(i, dm.acquire(ch, "me"), True, True, c, f"CAN{{ch}}"))
    if role == "recv":
        print("READY", flush=True)
        r = Receiver(links, path, c).run()
    else:
        time.sleep(1.0)
        r = Sender(links, c).run([path])
    print("STATE", r["state"], r.get("error", ""), r.get("files_done"), flush=True)
    dm.shutdown()
''')


class TwoProcesses(unittest.TestCase):
    def run_two(self, chans):
        d = tempfile.mkdtemp(); self.addCleanup(shutil.rmtree, d, True)
        port = free_port()
        env = dict(os.environ, CANVDS_NETBUS_PORT=str(port))
        script = os.path.join(d, "child.py")
        open(script, "w").write(CHILD.format(root=os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))))
        src = os.path.join(d, "src.bin"); r = random.Random(3)
        with open(src, "wb") as f:
            f.write(bytes(r.getrandbits(8) for _ in range(4096)) * 100)                   # 400 KB
        dst = os.path.join(d, "dst")
        rx = subprocess.Popen([sys.executable, script, "recv", dst, chans], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(rx.kill)
        self.assertIn("READY", rx.stdout.readline())
        tx = subprocess.run([sys.executable, script, "send", src, chans], env=env, capture_output=True, text=True, timeout=120)
        out_rx = rx.communicate(timeout=60)[0]
        self.assertIn("STATE done", tx.stdout, tx.stdout + tx.stderr)
        self.assertIn("STATE done", out_rx)
        a, b = open(src, "rb").read(), open(os.path.join(dst, "src.bin"), "rb").read()
        self.assertEqual(a, b)

    def test_file_between_two_processes_single_channel(self):
        self.run_two("0")

    def test_file_between_two_processes_dual_channel(self):
        self.run_two("0,1")


if __name__ == "__main__":
    unittest.main()
