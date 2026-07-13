# -*- coding: utf-8 -*-
"""车机模拟器:按标准 JT808 走 注册→鉴权→1秒1包位置汇报(带 0xF1 陀螺仪扩展)。

用法(在项目根目录):
    uv run python scripts/simulator.py                        # 连本机
    uv run python scripts/simulator.py --host 186.244.238.6   # 连服务器
    uv run python scripts/simulator.py --device 13912344321 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import math
import random
import struct
import sys
import time
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from tracker import jt808  # noqa: E402

# 深圳南山一圈虚拟路线(WGS84 经degrees),按顺序循环行驶
ROUTE = [
    (113.9210, 22.5250),
    (113.9330, 22.5255),
    (113.9420, 22.5310),
    (113.9455, 22.5395),
    (113.9380, 22.5460),
    (113.9260, 22.5455),
    (113.9185, 22.5390),
    (113.9175, 22.5300),
]
SPEED_KMH = 32.0
DEG_PER_METER = 1 / 111320.0  # 粗略换算


def bcd_now() -> bytes:
    return bytes.fromhex(datetime.now().strftime("%y%m%d%H%M%S"))


class RouteWalker:
    """沿路线匀速行驶,输出位置/航向/角速度。"""

    def __init__(self) -> None:
        self.idx = 0
        self.pos = ROUTE[0]
        self.direction = 0.0
        self.prev_direction = 0.0
        self.speed = SPEED_KMH

    def step(self, dt: float) -> tuple[float, float, float, int]:
        target = ROUTE[(self.idx + 1) % len(ROUTE)]
        lon, lat = self.pos
        dx = (target[0] - lon) / DEG_PER_METER * math.cos(math.radians(lat))
        dy = (target[1] - lat) / DEG_PER_METER
        dist = math.hypot(dx, dy)
        self.speed = max(15.0, min(45.0, self.speed + random.uniform(-2, 2)))
        step_m = self.speed / 3.6 * dt
        if dist <= step_m:
            self.idx = (self.idx + 1) % len(ROUTE)
            self.pos = target
        else:
            ratio = step_m / dist
            self.pos = (lon + (target[0] - lon) * ratio, lat + (target[1] - lat) * ratio)
        self.prev_direction = self.direction
        self.direction = (math.degrees(math.atan2(dx, dy)) + 360) % 360
        return self.pos[0], self.pos[1], self.speed, int(self.direction)

    def gyro(self, dt: float) -> bytes:
        # 角速度:航向变化率(0.1°/s);加速度:mG,静止 z≈1000
        yaw_rate = (self.direction - self.prev_direction + 540) % 360 - 180
        gz = int(max(-32000, min(32000, yaw_rate / dt * 10)))
        gx = random.randint(-30, 30)
        gy = random.randint(-50, 50)
        ax = random.randint(-80, 80)
        ay = random.randint(-120, 120)
        az = 1000 + random.randint(-40, 40)
        return jt808.build_gyro_extra(gx, gy, gz, ax, ay, az)


async def read_frames(reader: asyncio.StreamReader, buffer: bytearray):
    chunk = await reader.read(4096)
    if not chunk:
        raise ConnectionError("服务端关闭了连接")
    buffer += chunk
    return [jt808.parse_frame(seg) for seg in jt808.split_frames(buffer)]


async def wait_for(reader: asyncio.StreamReader, buffer: bytearray, msg_id: int, timeout: float = 8.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        for msg in await asyncio.wait_for(read_frames(reader, buffer), timeout):
            if msg.msg_id == msg_id:
                return msg
    raise TimeoutError(f"等待 0x{msg_id:04X} 超时")


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18808)
    ap.add_argument("--device", default="13912344321", help="终端手机号(设备ID)")
    ap.add_argument("--interval", type=float, default=1.0)
    ap.add_argument("--duration", type=float, default=0, help="运行秒数,0=一直跑")
    args = ap.parse_args()

    reader, writer = await asyncio.open_connection(args.host, args.port)
    buffer = bytearray()
    serial = 0

    def next_serial() -> int:
        nonlocal serial
        serial = (serial + 1) & 0xFFFF
        return serial

    # 1. 注册 0x0100(2013 版):省域+市县域+制造商ID(5)+型号(20)+终端ID(7)+车牌颜色+车牌
    reg_body = struct.pack(">HH", 0x002C, 0x012C)
    reg_body += b"WMPTK"                      # 制造商 ID
    reg_body += b"WAIMAI-CARBOX-01".ljust(20, b"\x00")  # 终端型号
    reg_body += args.device[-7:].encode().rjust(7, b"0")  # 终端 ID
    reg_body += bytes([1]) + "粤B·外卖01".encode("gbk")
    writer.write(jt808.build_frame(jt808.MSG_REGISTER, args.device, next_serial(), reg_body))
    await writer.drain()
    resp = await wait_for(reader, buffer, jt808.MSG_REGISTER_RESP)
    result = resp.body[2]
    auth_code = resp.body[3:].decode("gbk")
    print(f"[reg ] 注册应答 result={result} auth_code={auth_code}", flush=True)

    # 2. 鉴权 0x0102
    writer.write(jt808.build_frame(jt808.MSG_AUTH, args.device, next_serial(), auth_code.encode("gbk")))
    await writer.drain()
    resp = await wait_for(reader, buffer, jt808.MSG_PLATFORM_GENERAL_RESP)
    print(f"[auth] 鉴权应答 result={resp.body[4]}", flush=True)

    # 3. 1 秒 1 包位置汇报
    walker = RouteWalker()
    started = time.time()
    sent = 0
    print(f"[run ] 开始上报位置,每 {args.interval}s 一包(Ctrl+C 停止)", flush=True)
    while True:
        lon, lat, speed, direction = walker.step(args.interval)
        body = jt808.build_location_body(
            lat=lat,
            lon=lon,
            speed_kmh=speed,
            direction=direction,
            gps_time_bcd=bcd_now(),
            altitude=12,
            extras=walker.gyro(args.interval),
        )
        writer.write(jt808.build_frame(jt808.MSG_LOCATION, args.device, next_serial(), body))
        await writer.drain()
        sent += 1
        if sent % 10 == 1:
            print(f"[loc ] #{sent} ({lat:.6f}, {lon:.6f}) {speed:.1f}km/h dir={direction}", flush=True)
        # 消费平台应答,避免缓冲堆积
        try:
            await asyncio.wait_for(read_frames(reader, buffer), 0.05)
        except (asyncio.TimeoutError, TimeoutError):
            pass
        if args.duration and time.time() - started >= args.duration:
            break
        await asyncio.sleep(args.interval)

    writer.close()
    print(f"[done] 共上报 {sent} 个位置点", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
