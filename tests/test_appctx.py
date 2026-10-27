import os, sys, tempfile, unittest
sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
from zdiag_ui.appctx import AppContext, ServiceDefs, ConnError
from zdiag.project import SIM_DEVICE, Addressing


class Ctx(unittest.TestCase):
    def test_config_roundtrip(self):
        d = tempfile.mkdtemp(); c = AppContext(d)
        c.config.channel.device = "USBCAN-II"; c.config.channel.bitrate = 250000
        c.config.diag.addr.phys = 0x7E3; c.config.diag.settings.p2_ms = 1234
        c.save(); c2 = AppContext(d)
        self.assertEqual((c2.config.channel.device, c2.config.channel.bitrate), ("USBCAN-II", 250000))
        self.assertEqual((c2.config.diag.addr.phys, c2.config.diag.settings.p2_ms), (0x7E3, 1234))

    def test_services_defaults_and_save(self):
        d = tempfile.mkdtemp(); c = AppContext(d)
        self.assertEqual(c.services.find(0x10)["name"], "诊断会话控制")
        self.assertEqual(len(c.services.find(0x10)["subs"]), 3)
        c.services.items.append({"sid": 0xBA, "name": "自定义", "subs": [{"code": 1, "name": "x"}]})
        c.services.save(); self.assertEqual(ServiceDefs(c.services.path).find(0xBA)["name"], "自定义")

    def test_broken_files_fall_back(self):
        d = tempfile.mkdtemp()
        open(os.path.join(d, "myzcanpro_config.json"), "w").write("{broken")
        open(os.path.join(d, "myzcanpro_services.json"), "w").write("[1,2")
        c = AppContext(d); self.assertEqual(c.config.diag.addr.phys, 0x7E2); self.assertTrue(c.services.items)

    def test_sim_connection_and_exclusive_owner(self):
        c = AppContext(tempfile.mkdtemp()); c.config.channel.device = SIM_DEVICE
        b1 = c.conn.open(c.config.channel, Addressing()); b2 = c.conn.open(c.config.channel, Addressing())
        self.assertIs(b1, b2)
        c.conn.claim("UDS诊断")
        with self.assertRaises(ConnError): c.conn.claim("ECU刷新")
        c.conn.release("UDS诊断"); c.conn.claim("ECU刷新")
        c.conn.close()

    def test_real_device_errors_are_readable(self):
        c = AppContext(tempfile.mkdtemp())
        with self.assertRaises(ConnError) as e: c.conn.open(c.config.channel, Addressing())
        self.assertTrue(str(e.exception))


if __name__ == "__main__":
    unittest.main()
