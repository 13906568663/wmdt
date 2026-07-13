"""外卖平台·车辆轨迹服务入口。

一个进程带起三个端口:
- TCP  18808:JT808 硬件接入(1 秒 1 包位置汇报,含 0xF1 陀螺仪扩展)
- TCP  18883:MQTT 硬件接入(JSON 上报,含雷达/刹车/陀螺仪字段)
- HTTP 18209:百度地图轨迹页面 + REST API

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
BAIDU_AK = os.getenv("BAIDU_MAP_AK", "mS6xcVAJ12xyvGDiJiTH0dxVHnoWFeYf")

STATIC_DIR = Path(__file__).parent / "tracker" / "static"

storage = Storage()
tcp_server = JT808Server(storage, host=TCP_HOST, port=TCP_PORT)
mqtt_server = MQTTServer(storage, host=TCP_HOST, port=MQTT_PORT)


@asynccontextmanager
async def lifespan(app: FastAPI):
    await tcp_server.start()
    await mqtt_server.start()
    yield
    await mqtt_server.stop()
    await tcp_server.stop()


app = FastAPI(title="外卖平台·车辆轨迹服务", lifespan=lifespan)
app.include_router(build_router(storage), prefix="/api")
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")


@app.get("/api/config")
def get_config():
    return {"baidu_ak": BAIDU_AK, "tcp_port": TCP_PORT, "mqtt_port": MQTT_PORT}


@app.get("/", include_in_schema=False)
def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html")


def run() -> None:
    uvicorn.run(app, host=WEB_HOST, port=WEB_PORT, log_level="info")


if __name__ == "__main__":
    run()
