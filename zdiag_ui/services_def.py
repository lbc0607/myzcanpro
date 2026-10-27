"""UDS service catalogue shown in the service tree / flash editor (editable, stored as JSON)."""
import copy
import json
import os
from typing import Dict, List


def _s(code, name):
    return {"code": code, "name": name}


DEFAULT_SERVICES: List[Dict] = [
    {"sid": 0x10, "name": "诊断会话控制", "subs": [_s(1, "默认会话"), _s(2, "编程会话"), _s(3, "扩展会话")]},
    {"sid": 0x11, "name": "ECU重置", "subs": [_s(1, "硬复位"), _s(2, "钥匙关开复位"), _s(3, "软复位")]},
    {"sid": 0x14, "name": "清除诊断信息", "subs": []},
    {"sid": 0x19, "name": "读取DTC信息", "subs": [_s(1, "按状态掩码读取DTC数量"), _s(2, "按状态掩码读取DTC"),
                                               _s(4, "读取DTC快照记录"), _s(6, "读取DTC扩展数据记录"),
                                               _s(0x0A, "读取所有支持的DTC")]},
    {"sid": 0x22, "name": "按标识符读取数据", "subs": []},
    {"sid": 0x23, "name": "按地址读取内容", "subs": []},
    {"sid": 0x24, "name": "按标识符读取换算数据", "subs": []},
    {"sid": 0x27, "name": "安全访问", "subs": [_s(1, "请求种子(级别1)"), _s(2, "发送密钥(级别1)"),
                                             _s(3, "请求种子(级别3)"), _s(4, "发送密钥(级别3)"),
                                             _s(5, "请求种子(级别5)"), _s(6, "发送密钥(级别5)")]},
    {"sid": 0x28, "name": "通讯控制", "subs": [_s(0, "使能收发"), _s(1, "使能接收禁止发送"),
                                             _s(2, "禁止接收使能发送"), _s(3, "禁止收发")]},
    {"sid": 0x2A, "name": "按周期性标识符读取数据", "subs": []},
    {"sid": 0x2C, "name": "动态定义数据标识符", "subs": [_s(1, "按标识符定义"), _s(2, "按内存地址定义"), _s(3, "清除")]},
    {"sid": 0x2E, "name": "按标识符写数据", "subs": []},
    {"sid": 0x2F, "name": "按标识符的输入输出控制", "subs": []},
    {"sid": 0x31, "name": "例程控制", "subs": [_s(1, "启动例程"), _s(2, "停止例程"), _s(3, "请求例程结果")]},
    {"sid": 0x34, "name": "文件下载", "subs": []},
    {"sid": 0x3D, "name": "按地址写内存", "subs": []},
    {"sid": 0x83, "name": "访问计时参数", "subs": [_s(1, "读取扩展计时参数"), _s(2, "恢复默认计时参数"),
                                                 _s(3, "读取当前计时参数"), _s(4, "设置计时参数")]},
    {"sid": 0x84, "name": "受保护的数据传输", "subs": []},
    {"sid": 0x85, "name": "控制DTC设置", "subs": [_s(1, "打开DTC记录"), _s(2, "关闭DTC记录")]},
    {"sid": 0x86, "name": "基于事件响应", "subs": [_s(0, "停止响应事件"), _s(1, "DTC状态变化时响应"),
                                               _s(3, "定时器中断时响应")]},
    {"sid": 0x87, "name": "链路控制", "subs": [_s(1, "验证波特率(固定)"), _s(2, "验证波特率(指定)"), _s(3, "切换波特率")]},
]


def default_services() -> List[Dict]:
    return copy.deepcopy(DEFAULT_SERVICES)


def load_services(path: str) -> List[Dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        out = []
        for s in data["services"]:
            out.append({"sid": int(s["sid"]), "name": str(s["name"]),
                        "subs": [{"code": int(x["code"]), "name": str(x["name"])} for x in s.get("subs", [])]})
        if out:
            return out
    except (OSError, ValueError, KeyError, TypeError):
        pass
    return default_services()


def save_services(path: str, services: List[Dict]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump({"services": services}, f, ensure_ascii=False, indent=2)


def find_service(services: List[Dict], sid: int):
    for s in services:
        if s["sid"] == sid:
            return s
    return None
