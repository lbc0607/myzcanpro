"""gs_usb 探测工具（Windows，32 位 Python；需要 pip install pyusb 和 32 位 libusb-1.0.dll）。

  python tools/gsusb_probe.py            只读：列出设备、固件能力、时钟、位时序限制，以及 500k / 5M 等波特率的计算结果（不发送任何帧）
  python tools/gsusb_probe.py --loop     再做一次自环测试：控制器内部回环(不接总线也行)，发 2000 个 FD 帧，统计回显/接收和速度
  python tools/gsusb_probe.py --loop --pack 4    同上，但每次 USB 写入 4 帧（测试固件是否支持多帧合并；默认 1）
把整个输出发给开发者即可。libusb-1.0.dll 放在本脚本同级、上一级或 vendor 目录均可。
"""
import argparse
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(HERE, ".."))
from zdiag import gsusb as G                      # noqa: E402
from zdiag.bus import CanFrame                    # noqa: E402
from zdiag.busstats import ChannelCfg             # noqa: E402


def feat(v):
    names = [(G.F_LISTEN_ONLY, "只听"), (G.F_LOOP_BACK, "回环"), (G.F_TRIPLE_SAMPLE, "三次采样"), (G.F_ONE_SHOT, "单次发送"),
             (G.F_HW_TIMESTAMP, "硬件时间戳"), (G.F_IDENTIFY, "识别灯"), (G.F_USER_ID, "用户ID"), (G.F_PAD_PKTS, "填充包"),
             (G.F_FD, "CAN FD"), (1 << 9, "REQ_USB_QUIRK_LPC546XX"), (G.F_BT_CONST_EXT, "BT_CONST_EXT"), (G.F_TERMINATION, "软件终端电阻"),
             (G.F_BERR_REPORTING, "总线错误上报"), (G.F_GET_STATE, "读状态")]
    return "、".join(n for b, n in names if v & b) or "无"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", action="store_true")
    ap.add_argument("--pack", type=int, default=1)
    ap.add_argument("--index", type=int, default=0)
    a = ap.parse_args()
    base = os.path.abspath(os.path.join(HERE, ".."))
    for d in (HERE, base):
        if G.find_libusb(d):
            base = d
            break
    print("Python", sys.version.split()[0], "位数", 8 * __import__("struct").calcsize("P"), "libusb:", G.find_libusb(base) or "(系统路径)")
    devs = G.list_usb_devices(base)
    print("找到 gs_usb 设备:", len(devs))
    for d in devs:
        print("  #%d  %04X:%04X  bus %s addr %s  产品=%s 序列号=%s" % (d["index"], d["vid"], d["pid"], d["bus"], d["address"], d["product"], d["serial"]))
    if not devs:
        return 1
    dev = G.GsUsbDevice("probe", a.index, base)
    try:
        i = dev.info()
        print("通道数:", i["channels"], " 固件 sw=0x%X hw=0x%X" % (i["sw"], i["hw"]), " CAN 时钟 %.1f MHz" % (i["fclk"] / 1e6))
        print("能力:", feat(i["feature"]))
        for name, c in (("仲裁域", i["nominal"]), ("数据域", i["data"])):
            if c:
                print("%s限制: tseg1 %d..%d tseg2 %d..%d sjw<=%d brp %d..%d 步进 %d" % (name, c["tseg1_min"], c["tseg1_max"], c["tseg2_min"], c["tseg2_max"],
                                                                         c["sjw_max"], c["brp_min"], c["brp_max"], c["brp_inc"]))
        print("位时序计算（采样点按默认规则）:")
        for rate, lim in ((1_000_000, i["nominal"]), (500_000, i["nominal"]), (250_000, i["nominal"]),
                          (5_000_000, i["data"] or i["nominal"]), (2_000_000, i["data"] or i["nominal"])):
            try:
                b = G.calc_bittiming(i["fclk"], rate, G.auto_sample_pct(rate), lim)
                print("  %7.0f kbaud/s: brp=%d tseg1=%d tseg2=%d sjw=%d 采样点=%.1f%%" % (rate / 1000, b["brp"], b["prop_seg"] + b["phase_seg1"], b["phase_seg2"], b["sjw"], b["sample_pct"]))
            except ValueError as e:
                print("  %7.0f kbaud/s: %s" % (rate / 1000, e))
        if a.loop:
            G.TX_PACK = max(1, a.pack)
            fd = bool(i["feature"] & G.F_FD)
            cfg = ChannelCfg(protocol="CAN FD" if fd else "CAN", bitrate_kbps=500, data_kbps=2000, terminator=False)
            bus = dev.start_channel(0, cfg, None, G.F_LOOP_BACK)
            n, got, t0 = 2000, 0, time.time()
            sent = 0
            frames = [CanFrame(0x123, bytes((k + j) & 255 for j in range(64 if fd else 8)), fd=fd, brs=fd) for k in range(n)]
            while sent < n and time.time() - t0 < 20:
                k = bus.send_many(frames[sent:sent + 64])
                sent += k
                if not k:
                    time.sleep(0.0005)
                while bus.recv(0) is not None:
                    got += 1
            time.sleep(0.5)
            while bus.recv(0) is not None:
                got += 1
            dt = time.time() - t0 - 0.5
            print("自环: 发出 %d 帧(%s)，用时 %.2f 秒 = %.0f 帧/秒，回显确认 %d，收到 %d；每次 USB 写 %d 帧" %
                  (sent, "FD 64字节" if fd else "经典8字节", dt, sent / max(dt, 1e-6), bus.stats.tx, got, G.TX_PACK))
            print("事件:", "; ".join(t for _, t in bus.events))
            dev.stop_channel(0)
    finally:
        dev.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
