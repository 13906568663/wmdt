"""JT/T 808 编解码(服务端所需子集)。

支持:
- 0x7e 定界与 0x7d 转义、异或校验;
- 2013 / 2019 两版消息头(按消息体属性 bit14 版本标识自动区分);
- 终端侧消息解析:0x0100 注册 / 0x0102 鉴权 / 0x0002 心跳 / 0x0003 注销 /
  0x0200 位置汇报(含附加信息);
- 平台侧消息构造:0x8001 平台通用应答 / 0x8100 注册应答;
- 附加信息 0xF1:陀螺仪扩展(12 字节,三轴角速度 + 三轴加速度,int16 有符号)。
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Any

FLAG = 0x7E

MSG_HEARTBEAT = 0x0002
MSG_LOGOUT = 0x0003
MSG_REGISTER = 0x0100
MSG_AUTH = 0x0102
MSG_LOCATION = 0x0200

MSG_PLATFORM_GENERAL_RESP = 0x8001
MSG_REGISTER_RESP = 0x8100


class FrameError(Exception):
    pass


# ── 转义与校验 ─────────────────────────────────────────


def unescape(data: bytes) -> bytes:
    out = bytearray()
    i = 0
    while i < len(data):
        b = data[i]
        if b == 0x7D and i + 1 < len(data):
            nxt = data[i + 1]
            if nxt == 0x02:
                out.append(0x7E)
                i += 2
                continue
            if nxt == 0x01:
                out.append(0x7D)
                i += 2
                continue
        out.append(b)
        i += 1
    return bytes(out)


def escape(data: bytes) -> bytes:
    out = bytearray()
    for b in data:
        if b == 0x7E:
            out += b"\x7d\x02"
        elif b == 0x7D:
            out += b"\x7d\x01"
        else:
            out.append(b)
    return bytes(out)


def xor_checksum(data: bytes) -> int:
    c = 0
    for b in data:
        c ^= b
    return c


# ── 消息模型 ───────────────────────────────────────────


@dataclass
class Message:
    msg_id: int
    phone: str
    serial: int
    body: bytes
    version_2019: bool = False
    protocol_version: int = 0
    subpackage: tuple[int, int] | None = None  # (总包数, 包序号)


def split_frames(buffer: bytearray) -> list[bytes]:
    """从接收缓冲里切出完整帧(去掉 0x7e 定界符),留下残包。

    兼容两种流:相邻帧各自带定界符(...7e 7e...)和共享一个 0x7e 的紧凑流。
    """
    frames: list[bytes] = []
    while True:
        try:
            start = buffer.index(FLAG)
        except ValueError:
            buffer.clear()
            return frames
        try:
            end = buffer.index(FLAG, start + 1)
        except ValueError:
            if start > 0:
                del buffer[:start]
            return frames
        if end == start + 1:
            # 相邻两个 0x7e(上一帧尾 + 下一帧头),丢掉前一个继续
            del buffer[: start + 1]
            continue
        segment = bytes(buffer[start + 1 : end])
        del buffer[:end]  # 保留 end 处的 0x7e,它可能是下一帧的起始定界符
        frames.append(segment)


def parse_frame(segment: bytes) -> Message:
    """segment 为两个 0x7e 之间的原始字节(未去转义)。"""
    raw = unescape(segment)
    if len(raw) < 12:
        raise FrameError(f"帧过短: {len(raw)} 字节")
    body_and_header, checksum = raw[:-1], raw[-1]
    if xor_checksum(body_and_header) != checksum:
        raise FrameError("校验和不匹配")

    msg_id, props = struct.unpack_from(">HH", body_and_header, 0)
    body_len = props & 0x03FF
    subpackaged = bool(props & 0x2000)
    version_2019 = bool(props & 0x4000)

    offset = 4
    protocol_version = 0
    if version_2019:
        protocol_version = body_and_header[offset]
        offset += 1
        phone_bytes = body_and_header[offset : offset + 10]
        offset += 10
    else:
        phone_bytes = body_and_header[offset : offset + 6]
        offset += 6

    phone = phone_bytes.hex().lstrip("0") or "0"
    serial = struct.unpack_from(">H", body_and_header, offset)[0]
    offset += 2

    subpackage = None
    if subpackaged:
        total, index = struct.unpack_from(">HH", body_and_header, offset)
        offset += 4
        subpackage = (total, index)

    body = body_and_header[offset : offset + body_len]
    if len(body) != body_len:
        raise FrameError(f"消息体长度不符: 声明 {body_len},实际 {len(body)}")

    return Message(
        msg_id=msg_id,
        phone=phone,
        serial=serial,
        body=body,
        version_2019=version_2019,
        protocol_version=protocol_version,
        subpackage=subpackage,
    )


def build_frame(
    msg_id: int,
    phone: str,
    serial: int,
    body: bytes,
    version_2019: bool = False,
) -> bytes:
    props = len(body) & 0x03FF
    if version_2019:
        props |= 0x4000
        phone_bcd = bytes.fromhex(phone.rjust(20, "0"))
        header = struct.pack(">HHB", msg_id, props, 1) + phone_bcd + struct.pack(">H", serial)
    else:
        phone_bcd = bytes.fromhex(phone.rjust(12, "0"))
        header = struct.pack(">HH", msg_id, props) + phone_bcd + struct.pack(">H", serial)
    payload = header + body
    payload += bytes([xor_checksum(payload)])
    return bytes([FLAG]) + escape(payload) + bytes([FLAG])


# ── 平台应答构造 ───────────────────────────────────────


def build_general_response(msg: Message, result: int = 0, serial: int = 0) -> bytes:
    body = struct.pack(">HHB", msg.serial, msg.msg_id, result)
    return build_frame(MSG_PLATFORM_GENERAL_RESP, msg.phone, serial, body, msg.version_2019)


def build_register_response(msg: Message, auth_code: str, result: int = 0, serial: int = 0) -> bytes:
    body = struct.pack(">HB", msg.serial, result) + auth_code.encode("gbk")
    return build_frame(MSG_REGISTER_RESP, msg.phone, serial, body, msg.version_2019)


# ── 0x0200 位置汇报解析 ────────────────────────────────


def _bcd_time(data: bytes) -> str:
    """BCD[6] YYMMDDhhmmss(东八区)→ 'YYYY-MM-DD HH:MM:SS'。"""
    s = data.hex()
    return f"20{s[0:2]}-{s[2:4]}-{s[4:6]} {s[6:8]}:{s[8:10]}:{s[10:12]}"


def parse_location(body: bytes) -> dict[str, Any]:
    if len(body) < 28:
        raise FrameError(f"位置汇报消息体过短: {len(body)}")
    alarm, status, lat_raw, lon_raw, altitude, speed_raw, direction = struct.unpack_from(
        ">IIIIHHH", body, 0
    )
    gps_time = _bcd_time(body[22:28])

    lat = lat_raw / 1e6
    lon = lon_raw / 1e6
    if status & (1 << 2):  # 南纬
        lat = -lat
    if status & (1 << 3):  # 西经
        lon = -lon

    point: dict[str, Any] = {
        "alarm": alarm,
        "status": status,
        "acc_on": bool(status & 1),
        "located": bool(status & (1 << 1)),
        "lat": round(lat, 6),
        "lon": round(lon, 6),
        "altitude": altitude,
        "speed": round(speed_raw / 10.0, 1),  # 0.1 km/h
        "direction": direction,
        "gps_time": gps_time,
        "extras": {},
    }

    # 附加信息:id(1) + len(1) + data
    offset = 28
    while offset + 2 <= len(body):
        ext_id = body[offset]
        ext_len = body[offset + 1]
        data = body[offset + 2 : offset + 2 + ext_len]
        offset += 2 + ext_len
        if len(data) != ext_len:
            break
        _parse_extra(point, ext_id, data)
    return point


def _parse_extra(point: dict[str, Any], ext_id: int, data: bytes) -> None:
    extras: dict[str, Any] = point["extras"]
    if ext_id == 0xF1 and len(data) == 12:
        # 陀螺仪扩展:三轴角速度 + 三轴加速度,均为 int16 有符号大端
        gx, gy, gz, ax, ay, az = struct.unpack(">hhhhhh", data)
        point["gyro"] = {
            "gyro_x": gx,  # X轴角速度(左右侧翻/倾斜)
            "gyro_y": gy,  # Y轴角速度(前后俯仰/颠簸)
            "gyro_z": gz,  # Z轴角速度
            "acc_x": ax,   # X轴加速度(左右受力)
            "acc_y": ay,   # Y轴加速度(前后惯性,急刹车)
            "acc_z": az,   # Z轴加速度(垂直受力,静止约 1000mG)
        }
    elif ext_id == 0x01 and len(data) == 4:
        extras["mileage_km"] = struct.unpack(">I", data)[0] / 10.0
    elif ext_id == 0x30 and len(data) == 1:
        extras["rssi"] = data[0]
    elif ext_id == 0x31 and len(data) == 1:
        extras["satellites"] = data[0]
    else:
        extras[f"0x{ext_id:02X}"] = data.hex()


# ── 0xF1 构造(模拟器用) ──────────────────────────────


def build_gyro_extra(gx: int, gy: int, gz: int, ax: int, ay: int, az: int) -> bytes:
    return bytes([0xF1, 12]) + struct.pack(">hhhhhh", gx, gy, gz, ax, ay, az)


def build_location_body(
    lat: float,
    lon: float,
    speed_kmh: float,
    direction: int,
    gps_time_bcd: bytes,
    altitude: int = 10,
    acc_on: bool = True,
    extras: bytes = b"",
) -> bytes:
    status = (1 if acc_on else 0) | (1 << 1)  # ACC + 已定位
    body = struct.pack(
        ">IIIIHHH",
        0,
        status,
        int(round(abs(lat) * 1e6)),
        int(round(abs(lon) * 1e6)),
        altitude,
        int(round(speed_kmh * 10)),
        direction % 360,
    )
    return body + gps_time_bcd + extras
