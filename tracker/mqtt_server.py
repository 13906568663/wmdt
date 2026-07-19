"""内置 MQTT 接入服务(asyncio):另一类硬件按 JSON 上报,独立端口。

范围(服务端所需子集,协议 MQTT 3.1 / 3.1.1,兼容 5.0 的基本连接):
- CONNECT/CONNACK、PUBLISH(QoS0/1/2 收包侧)、SUBSCRIBE/SUBACK(便于调试订阅)、
  PING、UNSUBSCRIBE、DISCONNECT;
- 不落地会话/retain/遗嘱转发,纯接入用;
- 任意 topic 都接收,按 payload JSON 里的 devId 识别设备;
- 数据映射进与 JT808 相同的轨迹库,地图页直接可看。
"""

from __future__ import annotations

import asyncio
import json
import logging
import struct
import time
from typing import Any

from .events import EventDetector
from .geo import wgs84_to_bd09
from .rawlog import RawLogger
from .storage import DB_DIR, Storage

logger = logging.getLogger("mqtt.server")

CONNECT, CONNACK, PUBLISH, PUBACK, PUBREC, PUBREL, PUBCOMP = 1, 2, 3, 4, 5, 6, 7
SUBSCRIBE, SUBACK, UNSUBSCRIBE, UNSUBACK, PINGREQ, PINGRESP, DISCONNECT = 8, 9, 10, 11, 12, 13, 14

# payload 中直接映射到轨迹点列的字段,其余原样进 extras
_MAPPED = {
    "devId", "ts", "latitude", "longitude", "spdGPS", "direction", "height",
    "gpsStatus", "acc", "gyroX", "gyroY", "gyroZ", "accX", "accY", "accZ",
}


def _encode_varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n % 128
        n //= 128
        out.append(b | 0x80 if n else b)
        if not n:
            return bytes(out)


def _decode_varint(data: bytes, offset: int) -> tuple[int, int]:
    mult, value = 1, 0
    for _ in range(4):
        b = data[offset]
        offset += 1
        value += (b & 0x7F) * mult
        if not b & 0x80:
            return value, offset
        mult *= 128
    raise ValueError("varint 过长")


def _packet(ptype: int, flags: int, body: bytes) -> bytes:
    return bytes([(ptype << 4) | flags]) + _encode_varint(len(body)) + body


def _topic_match(filt: str, topic: str) -> bool:
    if filt == "#":
        return True
    fp, tp = filt.split("/"), topic.split("/")
    for i, seg in enumerate(fp):
        if seg == "#":
            return True
        if i >= len(tp) or (seg != "+" and seg != tp[i]):
            return False
    return len(fp) == len(tp)


class _Conn:
    def __init__(self, writer: asyncio.StreamWriter, peer: str) -> None:
        self.writer = writer
        self.peer = peer
        self.client_id = ""
        self.device_id = ""
        self.level = 4          # 协议版本:3=3.1, 4=3.1.1, 5=5.0
        self.keepalive = 60
        self.filters: list[str] = []


class MQTTServer:
    def __init__(self, storage: Storage, host: str = "0.0.0.0", port: int = 18883) -> None:
        self.storage = storage
        self.host = host
        self.port = port
        self._server: asyncio.AbstractServer | None = None
        self._conns: set[_Conn] = set()
        self._rawlog = RawLogger(DB_DIR / "raw", prefix="mqtt")
        self._detector = EventDetector(storage)
        self._tz_fixed: set[str] = set()  # 已提示过时钟校正的设备(只记日志一次)

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, self.host, self.port)
        logger.info("MQTT 接入服务已启动 %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()
        self._rawlog.close()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peername = writer.get_extra_info("peername")
        peer = f"{peername[0]}:{peername[1]}" if peername else "?"
        conn = _Conn(writer, peer)
        self._conns.add(conn)
        logger.info("MQTT 新连接 %s", peer)
        try:
            while True:
                timeout = conn.keepalive * 1.5 if conn.keepalive else None
                try:
                    ptype, flags, body = await asyncio.wait_for(
                        self._read_packet(reader), timeout=timeout
                    )
                except asyncio.TimeoutError:
                    logger.info("MQTT 超时断开 %s (%s)", peer, conn.client_id)
                    break
                if not await self._dispatch(conn, ptype, flags, body):
                    break
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except Exception as e:  # 单连接异常不影响服务
            logger.warning("MQTT 连接异常 %s: %s", peer, e)
        finally:
            self._conns.discard(conn)
            writer.close()
            logger.info("MQTT 断开 %s (%s)", peer, conn.client_id or "未知")

    @staticmethod
    async def _read_packet(reader: asyncio.StreamReader) -> tuple[int, int, bytes]:
        b0 = (await reader.readexactly(1))[0]
        mult, length = 1, 0
        for _ in range(4):
            b = (await reader.readexactly(1))[0]
            length += (b & 0x7F) * mult
            if not b & 0x80:
                break
            mult *= 128
        else:
            raise ValueError("报文长度字段非法")
        body = await reader.readexactly(length) if length else b""
        return b0 >> 4, b0 & 0x0F, body

    async def _send(self, conn: _Conn, data: bytes) -> None:
        conn.writer.write(data)
        await conn.writer.drain()

    async def _dispatch(self, conn: _Conn, ptype: int, flags: int, body: bytes) -> bool:
        """返回 False 表示应断开连接。"""
        if ptype == CONNECT:
            self._parse_connect(conn, body)
            ack = b"\x00\x00\x00" if conn.level >= 5 else b"\x00\x00"
            await self._send(conn, _packet(CONNACK, 0, ack))
            logger.info("MQTT 连接建立 %s client_id=%s v%s", conn.peer, conn.client_id, conn.level)
            return True

        if ptype == PUBLISH:
            qos = (flags >> 1) & 3
            topic, pid, payload = self._parse_publish(conn, flags, body)
            self._ingest(conn, topic, payload)
            if qos == 1 and pid is not None:
                await self._send(conn, _packet(PUBACK, 0, struct.pack(">H", pid)))
            elif qos == 2 and pid is not None:
                await self._send(conn, _packet(PUBREC, 0, struct.pack(">H", pid)))
            await self._forward(topic, payload, exclude=conn)
            return True

        if ptype == PUBREL:
            (pid,) = struct.unpack_from(">H", body, 0)
            await self._send(conn, _packet(PUBCOMP, 0, struct.pack(">H", pid)))
            return True

        if ptype == SUBSCRIBE:
            pid, filters = self._parse_subscribe(conn, body)
            conn.filters.extend(filters)
            codes = bytes(len(filters))  # 全部授予 QoS0
            head = struct.pack(">H", pid) + (b"\x00" if conn.level >= 5 else b"")
            await self._send(conn, _packet(SUBACK, 0, head + codes))
            return True

        if ptype == UNSUBSCRIBE:
            (pid,) = struct.unpack_from(">H", body, 0)
            head = struct.pack(">H", pid) + (b"\x00" if conn.level >= 5 else b"")
            await self._send(conn, _packet(UNSUBACK, 0, head))
            return True

        if ptype == PINGREQ:
            await self._send(conn, _packet(PINGRESP, 0, b""))
            return True

        if ptype == DISCONNECT:
            return False

        logger.info("MQTT 未处理报文类型 %s 来自 %s", ptype, conn.peer)
        return True

    # ── 报文解析 ───────────────────────────────────────

    @staticmethod
    def _parse_connect(conn: _Conn, body: bytes) -> None:
        (nlen,) = struct.unpack_from(">H", body, 0)
        off = 2 + nlen
        conn.level = body[off]
        off += 1
        off += 1  # connect flags
        conn.keepalive = struct.unpack_from(">H", body, off)[0]
        off += 2
        if conn.level >= 5:
            plen, off = _decode_varint(body, off)
            off += plen
        (cidlen,) = struct.unpack_from(">H", body, off)
        off += 2
        conn.client_id = body[off : off + cidlen].decode("utf-8", "replace")

    @staticmethod
    def _parse_publish(conn: _Conn, flags: int, body: bytes) -> tuple[str, int | None, bytes]:
        qos = (flags >> 1) & 3
        (tlen,) = struct.unpack_from(">H", body, 0)
        topic = body[2 : 2 + tlen].decode("utf-8", "replace")
        off = 2 + tlen
        pid = None
        if qos:
            (pid,) = struct.unpack_from(">H", body, off)
            off += 2
        if conn.level >= 5:
            plen, off = _decode_varint(body, off)
            off += plen
        return topic, pid, body[off:]

    @staticmethod
    def _parse_subscribe(conn: _Conn, body: bytes) -> tuple[int, list[str]]:
        (pid,) = struct.unpack_from(">H", body, 0)
        off = 2
        if conn.level >= 5:
            plen, off = _decode_varint(body, off)
            off += plen
        filters = []
        while off + 2 <= len(body):
            (flen,) = struct.unpack_from(">H", body, off)
            off += 2
            filters.append(body[off : off + flen].decode("utf-8", "replace"))
            off += flen + 1  # 跳过 QoS/订阅选项字节
        return pid, filters

    async def _forward(self, topic: str, payload: bytes, exclude: _Conn) -> None:
        """转发给调试订阅者(QoS0),接入本身不依赖。"""
        body = struct.pack(">H", len(topic.encode())) + topic.encode() + payload
        for c in list(self._conns):
            if c is exclude or not any(_topic_match(f, topic) for f in c.filters):
                continue
            try:
                pub_body = body
                if c.level >= 5:
                    head = struct.pack(">H", len(topic.encode())) + topic.encode() + b"\x00"
                    pub_body = head + payload
                await self._send(c, _packet(PUBLISH, 0, pub_body))
            except Exception:
                pass

    # ── 数据入库 ───────────────────────────────────────

    def _ingest(self, conn: _Conn, topic: str, payload: bytes) -> None:
        text = payload.decode("utf-8", "replace")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            self._rawlog.log_text("RX", conn.peer, conn.device_id, f"{topic} {text[:500]}")
            logger.warning("MQTT 非 JSON 消息 %s topic=%s", conn.peer, topic)
            return
        if not isinstance(data, dict):
            return
        dev = str(data.get("devId") or "").strip()
        self._rawlog.log_text("RX", conn.peer, dev, f"{topic} {text}")
        if not dev:
            logger.warning("MQTT 消息缺少 devId,忽略 topic=%s", topic)
            return
        conn.device_id = dev

        ts = float(data.get("ts") or time.time())
        if ts > 1e12:  # 毫秒兼容
            ts /= 1000
        # 部分刹车盒固件把北京时间当 UTC 秒上报,ts 比实际快 8 小时,
        # 服务端 localtime 再 +8 就成了"未来时间"。按接收时刻自动校正:
        # 偏差落在 8h±1h 窗口内才减 8(时钟正常的设备不受影响,固件修复后自动失效)。
        skew = ts - time.time()
        if 7 * 3600 <= skew <= 9 * 3600:
            ts -= 8 * 3600
            if dev not in self._tz_fixed:
                self._tz_fixed.add(dev)
                logger.info("MQTT 设备 %s 上报时间快 %.1f 小时,已自动 -8h 校正", dev, skew / 3600)
        lat = float(data.get("latitude") or 0)
        lon = float(data.get("longitude") or 0)
        point: dict[str, Any] = {
            "gps_time": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)),
            "lat": round(lat, 6),
            "lon": round(lon, 6),
            "altitude": int(data.get("height") or 0),
            "speed": float(data.get("spdGPS") or 0),
            "direction": int(data.get("direction") or 0),
            "acc_on": bool(data.get("acc") or 0),
            "located": bool(data.get("gpsStatus") or 0),
            "alarm": 0,
            "extras": {k: v for k, v in data.items() if k not in _MAPPED},
        }
        if data.get("gyroX") is not None:
            point["gyro"] = {
                "gyro_x": data.get("gyroX"),
                "gyro_y": data.get("gyroY"),
                "gyro_z": data.get("gyroZ"),
                "acc_x": data.get("accX"),
                "acc_y": data.get("accY"),
                "acc_z": data.get("accZ"),
            }
        lon_bd, lat_bd = wgs84_to_bd09(lon, lat)
        self.storage.upsert_device(dev, protocol="mqtt")
        self.storage.insert_point(dev, point, lon_bd=lon_bd, lat_bd=lat_bd)
        self._detector.process(dev, point, lon_bd=lon_bd, lat_bd=lat_bd)
