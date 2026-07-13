"""管理端 REST API:设备列表 / 最新位置 / 历史轨迹。"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from .storage import Storage


def build_router(storage: Storage) -> APIRouter:
    router = APIRouter()

    @router.get("/devices")
    def list_devices():
        return {"devices": storage.list_devices()}

    @router.get("/devices/{device_id}/latest")
    def latest(device_id: str):
        point = storage.latest_point(device_id)
        if point is None:
            raise HTTPException(404, f"设备 {device_id} 还没有轨迹数据")
        return point

    @router.get("/devices/{device_id}/track")
    def track(
        device_id: str,
        since_id: int = Query(default=0, ge=0, description="增量拉取:只要 id 大于该值的点"),
        start: str | None = Query(default=None, description="开始时间 YYYY-MM-DD HH:MM:SS"),
        end: str | None = Query(default=None, description="结束时间"),
        limit: int = Query(default=5000, ge=1, le=50000),
        all: int = Query(default=0, description="1=包含未定位的点(默认只返回定位有效的点)"),
    ):
        points = storage.track(
            device_id,
            since_id=since_id,
            start=start,
            end=end,
            limit=limit,
            only_located=not all,
        )
        return {"device_id": device_id, "count": len(points), "points": points}

    return router
