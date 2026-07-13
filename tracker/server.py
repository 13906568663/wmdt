"""JT808 TCP 接入服务(asyncio):注册/鉴权/心跳/位置汇报,1 秒 1 包。"""

from __future__ import annotations

import asyncio
import logging

from . import jt808
from .geo import wgs84_to_bd09
from .storage import Storage

logger = logging.getLogger("jt808.server")


class JT808Server:
    def __init__(self, storage: Storage, host: str = "0.0.0.0", port: int = 18808) -> None:
        self.storage = storage
        self.host = host
        self.port = port
        self._serial = 0
        self._server: asyncio.AbstractServer | None = None

    def _next_serial(self) -> int:
        self._serial = (self._serial + 1) & 0xFFFF
        return self._serial

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle_conn, self.host, self.port)
        logger.info("JT808 TCP 服务已启动 %s:%s", self.host, self.port)

    async def stop(self) -> None:
        if self._server:
            self._server.close()
            await self._server.wait_closed()

    async def _handle_conn(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        peer = writer.get_extra_info("peername")
        buffer = bytearray()
        device_id = ""
        logger.info("新连接 %s", peer)
        try:
            while True:
                chunk = await reader.read(4096)
                if not chunk:
                    break
                buffer += chunk
                for segment in jt808.split_frames(buffer):
                    try:
                        msg = jt808.parse_frame(segment)
                    except jt808.FrameError as e:
                        logger.warning("坏帧(%s): %s | %s", peer, e, segment.hex()[:80])
                        continue
                    device_id = msg.phone
                    reply = self._dispatch(msg)
                    if reply:
                        writer.write(reply)
                        await writer.drain()
        except (ConnectionResetError, asyncio.IncompleteReadError):
            pass
        finally:
            writer.close()
            logger.info("连接断开 %s (设备 %s)", peer, device_id or "未知")

    def _dispatch(self, msg: jt808.Message) -> bytes | None:
        if msg.msg_id == jt808.MSG_REGISTER:
            auth_code = f"AUTH{msg.phone[-6:]}"
            self.storage.upsert_device(msg.phone, auth_code=auth_code)
            logger.info("设备注册 %s,下发鉴权码 %s", msg.phone, auth_code)
            return jt808.build_register_response(msg, auth_code, serial=self._next_serial())

        if msg.msg_id == jt808.MSG_AUTH:
            self.storage.upsert_device(msg.phone)
            logger.info("设备鉴权 %s", msg.phone)
            return jt808.build_general_response(msg, serial=self._next_serial())

        if msg.msg_id == jt808.MSG_HEARTBEAT:
            self.storage.touch_device(msg.phone)
            return jt808.build_general_response(msg, serial=self._next_serial())

        if msg.msg_id == jt808.MSG_LOCATION:
            try:
                point = jt808.parse_location(msg.body)
            except jt808.FrameError as e:
                logger.warning("位置解析失败 %s: %s", msg.phone, e)
                return jt808.build_general_response(msg, result=2, serial=self._next_serial())
            lon_bd, lat_bd = wgs84_to_bd09(point["lon"], point["lat"])
            self.storage.upsert_device(msg.phone)
            self.storage.insert_point(msg.phone, point, lon_bd=lon_bd, lat_bd=lat_bd)
            gyro = point.get("gyro")
            logger.debug(
                "位置 %s (%.6f, %.6f) %skm/h %s",
                msg.phone, point["lat"], point["lon"], point["speed"],
                f"gyro={gyro}" if gyro else "",
            )
            return jt808.build_general_response(msg, serial=self._next_serial())

        if msg.msg_id == jt808.MSG_LOGOUT:
            logger.info("设备注销 %s", msg.phone)
            return jt808.build_general_response(msg, serial=self._next_serial())

        logger.info("未处理消息 0x%04X 来自 %s,回通用应答", msg.msg_id, msg.phone)
        return jt808.build_general_response(msg, serial=self._next_serial())
