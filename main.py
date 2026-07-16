"""外卖平台·车辆轨迹服务入口。

一个进程带起:
- HTTP 18209:百度地图轨迹页面 + REST API
- TCP  18808:JT808 硬件接入(TRACKER_ENABLE_JT808=0 关闭)
- TCP  18883:MQTT 硬件接入(TRACKER_ENABLE_MQTT=0 关闭)

两类硬件需要完全隔离部署时,用开关各起一个实例(独立端口+独立数据目录),
见 README「部署到服务器」。

启动:cd 外卖平台目录 && uv sync && uv run python main.py
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from tracker.api import build_router
from tracker.mqtt_server import MQTTServer
from tracker.server import JT808Server
from tracker.storage import Storage

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")

WEB_HOST = os.getenv("TRACKER_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.getenv("TRACKER_WEB_PORT", "18209"))
TCP_HOST = os.getenv("TRACKER_TCP_HOST", "0.0.0.0")
TCP_PORT = int(os.getenv("TRACKER_TCP_PORT", "18808"))
MQTT_PORT = int(os.getenv("TRACKER_MQTT_PORT", "18883"))
ENABLE_JT808 = os.getenv("TRACKER_ENABLE_JT808", "1") == "1"
ENABLE_MQTT = os.getenv("TRACKER_ENABLE_MQTT", "1") == "1"
BAIDU_AK = os.getenv("BAIDU_MAP_AK", "mS6xcVAJ12xyvGDiJiTH0dxVHnoWFeYf")
# 派单系统地址(公网可达,浏览器直连拉订单叠加到地图);留空则页面不显示订单图层
DISPATCH_API = os.getenv("DISPATCH_API_URL", "").rstrip("/")
# 演示聚焦设备:设置后页面只显示这一台并自动选中
FOCUS_DEVICE = os.getenv("TRACKER_FOCUS_DEVICE", "").strip()

STATIC_DIR = Path(__file__).parent / "tracker" / "static"

storage = Storage()
tcp_server = JT808Server(storage, host=TCP_HOST, port=TCP_PORT) if ENABLE_JT808 else None
mqtt_server = MQTTServer(storage, host=TCP_HOST, port=MQTT_PORT) if ENABLE_MQTT else None


@asynccontextmanager
async def lifespan(app: FastAPI):
    if tcp_server:
        await tcp_server.start()
    if mqtt_server:
        await mqtt_server.start()
    yield
    if mqtt_server:
        await mqtt_server.stop()
    if tcp_server:
        await tcp_server.stop()


app = FastAPI(title="外卖平台·车辆轨迹服务", lifespan=lifespan)
app.include_router(build_router(storage), prefix="/api")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/config")
def get_config():
    return {
        "baidu_ak": BAIDU_AK,
        "tcp_port": TCP_PORT if ENABLE_JT808 else None,
        "mqtt_port": MQTT_PORT if ENABLE_MQTT else None,
        "dispatch_api": DISPATCH_API or None,
        "focus_device": FOCUS_DEVICE or None,
    }


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def run() -> None:
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info")


if __name__ == "__main__":
    run()
