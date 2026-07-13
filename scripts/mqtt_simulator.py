# -*- coding: utf-8 -*-
"""MQTT 硬件模拟器:按协议文档的 JSON 字段,MQTT 3.1.1 上报(默认 QoS1)。

用法(在项目根目录):
    uv run python scripts/mqtt_simulator.py                        # 连本机
    uv run python scripts/mqtt_simulator.py --host 186.244.238.6   # 连服务器
    uv run python scripts/mqtt_simulator.py --device 983104015411 --interval 10 --duration 60
"""

from __future__ import annotations

import argparse
import asyncio
import json
import math
import random
import struct
import time

# 深圳南山一圈虚拟路线(WGS84),与 JT808 模拟器错开一条路
ROUTE = [
    (113.9500, 22.5400),
    (113.9585, 22.5430),
    (113.9650, 22.5500),
    (113.9600, 22.5580),
    (113.9500, 22.5605),
    (113.9420, 22.5545),
    (113.9430, 22.5450),
]
DEG_PER_METER = 1 / 111320.0


def encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + encode_varint(len(body)) + body


def mqtt_str(s: str) -> bytes:
    raw = s.encode("utf-8")
    return struct.pack(">H", len(raw)) + raw


def build_connect(client_id: str, keepalive: int = 60) -> bytes:
    body = mqtt_str("MQTT") + bytes([4]) + bytes([0x02]) + struct.pack(">H", keepalive)
    body += mqtt_str(client_id)
    return packet(1, 0, body)


def build_publish(topic: str, payload: bytes, pid: int, qos: int = 1) -> bytes:
    body = mqtt_str(topic)
    if qos:
        body += struct.pack(">H", pid)
    return packet(3, qos << 1, body + payload)


class Walker:
    def __init__(self) -> None:
        self.idx = 0
        self.pos = ROUTE[0]
        self.direction = 0.0
        self.speed = 28.0

    def step(self, dt: float) -> None:
        target = ROUTE[(self.idx + 1) % len(ROUTE)]
        lon, lat = self.pos
        dx = (target[0] - lon) / DEG_PER_METER * math.cos(math.radians(lat))
        dy = (target[1] - lat) / DEG_PER_METER
        dist = math.hypot(dx, dy)
        self.speed = max(12.0, min(40.0, self.speed + random.uniform(-2, 2)))
        step_m = self.speed / 3.6 * dt
        if dist <= step_m:
            self.idx = (self.idx + 1) % len(ROUTE)
            self.pos = target
        else:
            r = step_m / dist
            self.pos = (lon + (target[0] - lon) * r, lat + (target[1] - lat) * r)
        self.direction = (math.degrees(math.atan2(dx, dy)) + 360) % 360


def build_payload(device: str, w: Walker) -> dict:
    lon, lat = w.pos
    return {
        "devId": device,
        "ts": int(time.time()),
        "dataType": 2,          # 状态信息
        "triggerMark": 0,       # 定时上报
        "selfCheck": 1,
        "acc": 1,
        "busSta": 0,
        "hazLight": 0,
        "sysSW": 1,
        "rightLight": 0,
        "leftLight": 0,
        "rvsGear": 1,
        "brake": 0,
        "spdBUS": int(w.speed),
        "spdGPS": int(w.speed),
        "rotSpd": 1500 + random.randint(-200, 200),
        "brakeRad": -1,
        "distLFR": round(random.uniform(3, 30), 1),
        "distRFR": round(random.uniform(3, 30), 1),
        "distS1R": round(random.uniform(1, 10), 1),
        "distS2R": round(random.uniform(1, 10), 1),
        "engLFR": random.randint(10, 120),
        "engRFR": random.randint(10, 120),
        "engS1R": random.randint(10, 120),
        "engS2R": random.randint(10, 120),
        "angLFR": -15.0, "angRFR": 15.0, "angS1R": -90.0, "angS2R": 90.0,
        "height": 15,
        "direction": int(w.direction),
        "longitude": round(lon, 6),
        "latitude": round(lat, 6),
        "gpsStatus": 1,
        "mileage1": 1234, "mileage2": 567890,
        "gpsMileage": 12.3,
        "fuelConsumption": 0.42, "avgFuelConsumption": 5.6, "avgSpd": 26.5,
        "brakeCount": 3, "obdFuelConsumption": 88.2, "videoStatus": 1,
        "brakeSpd": "[0,0,0,0,0]", "brakeError": 0,
        "ttcTime": 0, "relativeSpd": 0, "targetLength": 0, "targetWidth": 0,
        "followTime": 0, "brakeType": 0, "brakeDeceleration": 0,
        "breakingTime": 0, "decelerationValue": 0, "brakingEndSpeed": 0,
        "brakingStartTime": 0,
        "createTs": int(time.time()),
        "gyroX": round(random.uniform(-3, 3), 2),
        "gyroY": round(random.uniform(-5, 5), 2),
        "gyroZ": round(random.uniform(-10, 10), 2),
        "accX": round(random.uniform(-80, 80), 1),
        "accY": round(random.uniform(-120, 120), 1),
        "accZ": round(1000 + random.uniform(-40, 40), 1),
    }


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=18883)
    ap.add_argument("--device", default="983104015411")
    ap.add_argument("--topic", default="", help="默认 wmdt/up/{devId}")
    ap.add_argument("--interval", type=float, default=2.0, help="文档定时为 10s,联调默认 2s")
    ap.add_argument("--duration", type=float, default=0, help="运行秒数,0=一直跑")
    args = ap.parse_args()
    topic = args.topic or f"wmdt/up/{args.device}"

    reader, writer = await asyncio.open_connection(args.host, args.port)
    writer.write(build_connect(f"sim-{args.device}"))
    await writer.drain()
    resp = await asyncio.wait_for(reader.readexactly(4), 5)
    if resp[0] >> 4 != 2 or resp[3] != 0:
        raise RuntimeError(f"CONNACK 异常: {resp.hex()}")
    print(f"[conn] MQTT 已连接 {args.host}:{args.port},topic={topic}", flush=True)

    w = Walker()
    pid = 0
    sent = 0
    started = time.time()
    while True:
        w.step(args.interval)
        pid = (pid % 0xFFFF) + 1
        payload = json.dumps(build_payload(args.device, w), ensure_ascii=False).encode()
        writer.write(build_publish(topic, payload, pid))
        await writer.drain()
        sent += 1
        try:  # 消费 PUBACK
            await asyncio.wait_for(reader.read(4), 0.3)
        except asyncio.TimeoutError:
            pass
        if sent % 5 == 1:
            lon, lat = w.pos
            print(f"[pub ] #{sent} ({lat:.6f}, {lon:.6f}) {w.speed:.1f}km/h", flush=True)
        if args.duration and time.time() - started >= args.duration:
            break
        await asyncio.sleep(args.interval)

    writer.write(packet(14, 0, b""))
    await writer.drain()
    writer.close()
    print(f"[done] 共发布 {sent} 条", flush=True)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
