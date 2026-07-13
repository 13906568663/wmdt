"""管理端 REST API:设备列表 / 最新位置 / 历史轨迹。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .storage import Storage

# 漂移抑制阈值
JUMP_SPEED_MPS = 42.0   # 隐含速度 >150km/h 视为坐标突跳,丢弃
STILL_DIST_M = 20.0     # 位移小于 20 米
STILL_SPEED_KMH = 3.0   # 且上报速度低于 3km/h → 视为静止漂移,不延伸轨迹


def _dist_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * asin(sqrt(h))


def _suppress_drift(points: list[dict[str, Any]], anchor: dict[str, Any] | None) -> list[dict[str, Any]]:
    """静止漂移抑制 + 突跳点剔除。anchor 是增量拉取时客户端已画的最后一个点。"""
    kept: list[dict[str, Any]] = []
    last = anchor
    for p in points:
        if last is None:
            kept.append(p)
            last = p
            continue
        d = _dist_m(last, p)
        dt = max(p["server_ts"] - last["server_ts"], 0.001)
        implied_kmh = d / dt * 3.6
        if implied_kmh > JUMP_SPEED_MPS * 3.6:
            continue
        # 位移隐含速度与设备上报车速严重不符 → 多路径甩点(静止时最典型),丢弃
        if implied_kmh > max(3 * max(p["speed"], last["speed"]), 20.0):
            continue
        if d < STILL_DIST_M and p["speed"] < STILL_SPEED_KMH:
            continue
        kept.append(p)
        last = p
    return kept


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
        if not all:
            anchor = storage.point_by_id(device_id, since_id) if since_id else None
            points = _suppress_drift(points, anchor)
        return {"device_id": device_id, "count": len(points), "points": points}

    return router
