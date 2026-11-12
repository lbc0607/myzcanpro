"""LinMaster on the ZLG box's LIN channel (zlgcan.dll), UDS over LIN through the library's own LIN UDS service.

Structure layouts are copied from the SDK header you supplied (docs/zlgcan_sdk_headers/zlgcan.h, all inside #pragma pack(1)):
ZCAN_LIN_INIT_CONFIG, ZLIN_UDS_REQUEST, ZCANUdsRequestDataObj, ZCAN_UDS_RESPONSE; calls ZCAN_InitLIN / ZCAN_StartLIN / ZCAN_ResetLIN /
ZCAN_UDS_RequestEX (dataType DEF_LIN_UDS_DATA).  The library runs the LIN transport layer itself (NAD + PCI on 0x3C / 0x3D, multi-frame),
so this class only implements `uds_request`; raw frame access is not offered here.
The device must be opened first (设备管理 → 打开设备); the LIN channel is opened on that already opened device."""
import ctypes as C
import threading
from typing import Any, Dict

from .lin import LinError, LinMaster

STATUS_OK = 1
DEF_LIN_UDS_DATA = 2
UDS_ERR = {0x01: "响应超时", 0x02: "LIN 发送失败（检查 LIN 供电、波特率、接线）", 0x03: "请求被取消", 0x04: "抑制响应", 0x05: "设备忙",
           0x06: "请求参数错误", 0x64: "其它错误"}
RT_NEGATIVE, RT_POSITIVE, RT_NONE = 0, 1, 2


class ZCAN_LIN_INIT_CONFIG(C.Structure):
    _pack_ = 1
    _fields_ = [("linMode", C.c_ubyte), ("chkSumMode", C.c_ubyte), ("maxLength", C.c_ubyte), ("reserved", C.c_ubyte),
                ("linBaud", C.c_uint)]


class _SessionParam(C.Structure):
    _pack_ = 1
    _fields_ = [("p2_timeout", C.c_uint), ("enhanced_timeout", C.c_uint),
                ("check_any_negative_response", C.c_ubyte, 1), ("wait_if_suppress_response", C.c_ubyte, 1), ("flag", C.c_ubyte, 6),
                ("reserved0", C.c_ubyte * 7)]


class _TransParam(C.Structure):
    _pack_ = 1
    _fields_ = [("fill_byte", C.c_ubyte), ("st_min", C.c_ubyte), ("reserved0", C.c_ubyte * 6)]


class ZLIN_UDS_REQUEST(C.Structure):
    _pack_ = 1
    _fields_ = [("req_id", C.c_uint), ("channel", C.c_ubyte), ("suppress_response", C.c_ubyte), ("sid", C.c_ubyte), ("Nad", C.c_ubyte),
                ("reserved1", C.c_ubyte * 8), ("session_param", _SessionParam), ("trans_param", _TransParam),
                ("data", C.POINTER(C.c_ubyte)), ("data_len", C.c_uint), ("reserved2", C.c_uint)]


class _LinUdsData(C.Structure):
    _pack_ = 1
    _fields_ = [("req", C.POINTER(ZLIN_UDS_REQUEST)), ("reserved", C.c_ubyte * 24)]


class _ReqUnion(C.Union):
    _pack_ = 1
    _fields_ = [("zcanLINUdsData", _LinUdsData), ("raw", C.c_ubyte * 63)]


class ZCANUdsRequestDataObj(C.Structure):
    _pack_ = 1
    _fields_ = [("dataType", C.c_uint), ("data", _ReqUnion), ("reserved", C.c_ubyte * 32)]


class _Positive(C.Structure):
    _pack_ = 1
    _fields_ = [("sid", C.c_ubyte), ("data_len", C.c_uint)]


class _Negative(C.Structure):
    _pack_ = 1
    _fields_ = [("neg_code", C.c_ubyte), ("sid", C.c_ubyte), ("error_code", C.c_ubyte)]


class _RespUnion(C.Union):
    _pack_ = 1
    _fields_ = [("positive", _Positive), ("negative", _Negative), ("raw", C.c_ubyte * 8)]


class ZCAN_UDS_RESPONSE(C.Structure):
    _pack_ = 1
    _anonymous_ = ("u",)
    _fields_ = [("status", C.c_ubyte), ("reserved", C.c_ubyte * 6), ("type", C.c_ubyte), ("u", _RespUnion)]


class ZlgLinMaster(LinMaster):
    def __init__(self, device: Any, lin_index: int, baud: int = 19200, chksum: int = 1) -> None:
        self.device, self.lin_index, self.baud = device, int(lin_index), int(baud)
        self.lock = threading.Lock()
        self._req_id = 0
        L = self.lib = device.lib
        L.ZCAN_InitLIN.restype = C.c_void_p
        L.ZCAN_InitLIN.argtypes = [C.c_void_p, C.c_uint, C.POINTER(ZCAN_LIN_INIT_CONFIG)]
        for n in ("ZCAN_StartLIN", "ZCAN_ResetLIN"):
            getattr(L, n).argtypes = [C.c_void_p]
            getattr(L, n).restype = C.c_uint
        L.ZCAN_UDS_RequestEX.argtypes = [C.c_void_p, C.POINTER(ZCANUdsRequestDataObj), C.POINTER(ZCAN_UDS_RESPONSE),
                                         C.POINTER(C.c_ubyte), C.c_uint]
        L.ZCAN_UDS_RequestEX.restype = C.c_uint
        cfg = ZCAN_LIN_INIT_CONFIG(1, int(chksum), 8, 0, self.baud)               # master, checksum mode, max length 8
        self.handle = L.ZCAN_InitLIN(device.handle, self.lin_index, C.byref(cfg))
        if not self.handle:
            raise LinError(f"ZCAN_InitLIN 失败：LIN 通道 {self.lin_index} 初始化不了（设备是否带 LIN、通道号、波特率 {self.baud} 是否正确）")
        if L.ZCAN_StartLIN(self.handle) != STATUS_OK:
            L.ZCAN_ResetLIN(self.handle)
            raise LinError(f"ZCAN_StartLIN 失败（LIN 通道 {self.lin_index}）")

    # --- only the library-level UDS is offered
    def send_frame(self, frame_id: int, data: bytes) -> None:
        raise LinError("周立功绑定只提供 UDS over LIN（用 lin_request / lin_scan_*）")

    def read_frame(self, frame_id: int, timeout: float):
        raise LinError("周立功绑定只提供 UDS over LIN（用 lin_request / lin_scan_*）")

    def close(self) -> None:
        h, self.handle = getattr(self, "handle", None), None
        if h:
            try:
                self.lib.ZCAN_ResetLIN(h)
            except Exception:
                pass

    def uds_request(self, nad: int, pdu: bytes, p2: float) -> Dict[str, Any]:
        if not self.handle:
            raise LinError("LIN 通道已关闭")
        body = bytes(pdu[1:])
        buf = (C.c_ubyte * max(len(body), 1))(*body)
        req = ZLIN_UDS_REQUEST()
        self._req_id = (self._req_id + 1) & 0xFFFF
        req.req_id, req.channel, req.sid, req.Nad = self._req_id, self.lin_index, pdu[0], nad
        req.session_param.p2_timeout = max(int(p2 * 1000), 200)                    # the SDK asks for >= 200 ms
        req.session_param.enhanced_timeout = max(int(p2 * 1000), 5000)
        req.trans_param.fill_byte = 0xFF
        req.data, req.data_len = C.cast(buf, C.POINTER(C.c_ubyte)), len(body)
        obj = ZCANUdsRequestDataObj()
        obj.dataType = DEF_LIN_UDS_DATA
        obj.data.zcanLINUdsData.req = C.pointer(req)
        resp = ZCAN_UDS_RESPONSE()
        out = (C.c_ubyte * 4096)()
        with self.lock:
            ret = self.lib.ZCAN_UDS_RequestEX(self.handle, C.byref(obj), C.byref(resp), out, 4096)
        if ret != STATUS_OK:
            return {"ok": False, "error": f"ZCAN_UDS_RequestEX 返回 {ret}（1=OK，0=失败，4=不支持，5=缓冲区太小）"}
        if resp.status != 0:
            return {"ok": False, "error": UDS_ERR.get(resp.status, f"UDS 错误 0x{resp.status:02X}")}
        if resp.type == RT_POSITIVE:
            n = min(resp.positive.data_len, 4096)
            return {"ok": True, "data": bytes([resp.positive.sid]) + bytes(out[:n])}
        if resp.type == RT_NEGATIVE:
            nrc = resp.negative.error_code
            return {"ok": False, "nrc": nrc, "error": f"否定响应 NRC 0x{nrc:02X}", "data": bytes([0x7F, resp.negative.sid, nrc])}
        return {"ok": False, "error": "无响应"}


def open_zlg(device: Any, channel: int, baud: int = 19200, chksum: int = 1) -> LinMaster:
    if device is None:
        raise LinError("还没有打开周立功设备：先在设备管理里打开设备（模拟设备没有 LIN）")
    return ZlgLinMaster(device, channel, baud, chksum)
