"""管理端 REST API:设备列表 / 最新位置 / 历史轨迹。"""

from __future__ import annotations

from math import asin, cos, radians, sin, sqrt
from typing import Any

from fastapi import APIRouter, HTTPException, Query

from .storage import Storage

# 漂移抑制参数。多路径漂移会同时伪造位移和 GPS 速度(实测静止设备报 5km/h),
# 单点特征不可靠;改用"一段时间的净位移"判定运动状态:
# 静止漂移是在原地打转,60 秒净位移拉不开;真实骑行/步行净位移远超阈值。
JUMP_SPEED_KMH = 150.0   # 相邻点隐含速度上限,超过视为坐标突跳
MOVE_WINDOW_S = 60.0     # 运动判定窗口
MOVE_START_M = 50.0      # 窗口净位移超过 → 进入"移动中"
MOVE_STOP_M = 25.0       # 低于 → 回到"静止"(迟滞防抖)
MIN_SEG_M = 10.0         # 相邻轨迹顶点最小间距(消抖)


def _dist_m(a: dict[str, Any], b: dict[str, Any]) -> float:
    lat1, lon1, lat2, lon2 = map(radians, (a["lat"], a["lon"], b["lat"], b["lon"]))
    h = sin((lat2 - lat1) / 2) ** 2 + cos(lat1) * cos(lat2) * sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * asin(sqrt(h))


def _mark_moving(seq: list[dict[str, Any]]) -> list[bool]:
    """按 60 秒净位移 + 迟滞,给每个点标注"是否处于移动中"。"""
    flags: list[bool] = []
    state = False
    j = 0
    for i, p in enumerate(seq):
        while j < i and seq[j]["server_ts"] < p["server_ts"] - MOVE_WINDOW_S:
            j += 1
        ref = seq[j - 1] if j > 0 else seq[0]
        net = _dist_m(ref, p)
        if net >= MOVE_START_M:
            state = True
        elif net <= MOVE_STOP_M:
            state = False
        flags.append(state)
    return flags


def _suppress_drift(
    points: list[dict[str, Any]], context: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    """context:窗口之前的原始点(增量拉取时用于运动状态判定与锚点),不计入返回。"""
    seq = context + points
    if not seq:
        return []
    flags = _mark_moving(seq)
    kept: list[dict[str, Any]] = []
    last = context[-1] if context else None
    rejects = 0
    for i in range(len(context), len(seq)):
        p = seq[i]
        if not flags[i]:
            continue
        if last is not None:
            d = _dist_m(last, p)
            dt = max(p["server_ts"] - last["server_ts"], 0.001)
            if d / dt * 3.6 > JUMP_SPEED_KMH:
                rejects += 1
                if rejects >= 3:
                    # 连续多点稳定在远处:静默重锚,不画跨越连线
                    last = p
                    rejects = 0
                continue
            rejects = 0
            if d < MIN_SEG_M:
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
            context = storage.recent_located_before(device_id, since_id) if since_id else []
            points = _suppress_drift(points, context)
        return {"device_id": device_id, "count": len(points), "points": points}

    @router.get("/devices/{device_id}/events")
    def events(
        device_id: str,
        since_id: int = Query(default=0, ge=0),
        start: str | None = Query(default=None, description="开始时间 YYYY-MM-DD HH:MM:SS"),
        end: str | None = Query(default=None, description="结束时间"),
        limit: int = Query(default=100, ge=1, le=1000),
    ):
        items = storage.list_events(device_id, since_id=since_id, start=start, end=end, limit=limit)
        return {"device_id": device_id, "count": len(items), "events": items}

    return router
