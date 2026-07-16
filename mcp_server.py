"""外卖平台·车辆数据 MCP 查询服务(独立进程)。

把 JT808 / MQTT 两个轨迹实例的 REST API 聚合成一个 MCP 接入点,
暴露给 AI 平台(agent-flow)做语音查询:车在哪、今天跑了多少、
有没有摔车/急刹等。所有工具返回简短中文文本,方便大模型直接口播。

环境变量:
    TRACKER_JT808_API   JT808 实例地址,默认 http://wmdt:18209
    TRACKER_MQTT_API    MQTT 实例地址,默认 http://wmdt-mqtt:18209
    MCP_PORT            监听端口,默认 18210

启动:python mcp_server.py  →  MCP 接入点 http://<host>:18210/mcp(streamable-http)
"""

from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Annotated, Any

import httpx
import uvicorn
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import Field

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s - %(message)s")
logger = logging.getLogger("tracker.mcp")

JT808_API = os.getenv("TRACKER_JT808_API", "http://wmdt:18209").rstrip("/")
MQTT_API = os.getenv("TRACKER_MQTT_API", "http://wmdt-mqtt:18209").rstrip("/")
MCP_PORT = int(os.getenv("MCP_PORT", "18210"))
# 锁定单一设备:设置后所有工具只查这台车,忽略入参 device_id,不再要求用户/客户选择。
# 留空则保持多设备行为。
FIXED_DEVICE = os.getenv("TRACKER_FIXED_DEVICE", "").strip()

_DEVICE_ARG_DESC = (
    f"设备号;系统已锁定为 {FIXED_DEVICE},不填即可"
    if FIXED_DEVICE
    else "设备号,如 14808381029;不确定就先调 list_vehicles"
)

# (api_base, 硬件说明)
INSTANCES = [
    (JT808_API, "车机(JT808)"),
    (MQTT_API, "刹车盒(MQTT)"),
]

_http = httpx.Client(timeout=30)

INSTRUCTIONS = (
    "外卖平台车辆数据查询。平台接入两类硬件:车机(JT808 协议)和智能刹车盒(MQTT 协议),"
    "都上报 GPS 轨迹与陀螺仪数据,服务端实时判定摔车/急刹车/颠簸/长时间停驻事件。"
    "可查:车辆列表与在线状态、某辆车当前位置(含街道地址)、时间段轨迹摘要(里程/时长/速度)、"
    "安全事件记录。设备号即车辆标识,用户说'车'、'骑手'、'设备'都指它。"
)

if FIXED_DEVICE:
    INSTRUCTIONS += (
        f" 本平台当前只服务一台车(设备号 {FIXED_DEVICE}),所有查询默认就是这台,"
        "无需向用户确认或让用户选择设备,device_id 一律不用填。"
    )


# ──────────────────────────────────────────────────────────────
# REST 取数
# ──────────────────────────────────────────────────────────────


def _get(base: str, path: str, **params: Any) -> Any:
    r = _http.get(f"{base}{path}", params={k: v for k, v in params.items() if v not in (None, "")})
    r.raise_for_status()
    return r.json()


def _all_devices() -> list[dict[str, Any]]:
    """合并两实例的设备列表,附加 _api/_hw 字段标记来源。"""
    out: list[dict[str, Any]] = []
    for base, hw in INSTANCES:
        try:
            for d in _get(base, "/api/devices").get("devices", []):
                d["_api"], d["_hw"] = base, hw
                out.append(d)
        except Exception as exc:
            logger.warning("拉取设备列表失败 %s: %s", base, exc)
    return out


def _find_device(device_id: str) -> dict[str, Any] | None:
    for d in _all_devices():
        if d["device_id"] == device_id:
            return d
    return None


def _visible_devices() -> list[dict[str, Any]]:
    """列表类工具可见的设备:锁定单设备时只返回那一台。"""
    devices = _all_devices()
    if FIXED_DEVICE:
        return [d for d in devices if d["device_id"] == FIXED_DEVICE]
    return devices


def _effective_id(device_id: str) -> str:
    """锁定单设备时始终用锁定设备,否则用入参。"""
    return FIXED_DEVICE or (device_id or "").strip()


# ──────────────────────────────────────────────────────────────
# 逆地理(OSM Nominatim,免费公共服务,限速 1 次/秒 + 网格缓存)
# ──────────────────────────────────────────────────────────────

_geo_cache: dict[tuple[float, float], str] = {}
_geo_lock = threading.Lock()
_geo_last_call = 0.0


def _rev_geocode(lat: float, lon: float) -> str:
    """WGS-84 坐标 → 简短中文地址;失败时回退为坐标文本。"""
    if not lat and not lon:
        return "未定位"
    key = (round(lat, 3), round(lon, 3))  # ~100m 网格缓存
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    global _geo_last_call
    wait = _geo_last_call + 1.1 - time.time()
    if wait > 0:
        time.sleep(wait)
    _geo_last_call = time.time()
    try:
        r = _http.get(
            "https://nominatim.openstreetmap.org/reverse",
            params={"lat": lat, "lon": lon, "format": "jsonv2",
                    "accept-language": "zh-CN", "zoom": 17},
            headers={"User-Agent": "waimai-tracker-mcp/1.0"},
            timeout=8,
        )
        a = r.json().get("address", {})
        parts = [a.get(k, "") for k in ("city", "suburb", "quarter", "road")]
        text = "".join(p for p in parts if p) or r.json().get("display_name", "")
        text = text or f"北纬{lat:.4f} 东经{lon:.4f}"
    except Exception:
        text = f"北纬{lat:.4f} 东经{lon:.4f}"
    with _geo_lock:
        _geo_cache[key] = text
    return text


# ──────────────────────────────────────────────────────────────
# 小工具
# ──────────────────────────────────────────────────────────────

_DIRS = ["北", "东北", "东", "东南", "南", "西南", "西", "西北"]

EVENT_NAMES = {
    "fall": "摔车",
    "fall_suspect": "疑似摔车",
    "hard_brake": "急刹车",
    "bump": "颠簸路段",
    "stop_long": "长时间停驻",
}


def _dir_text(deg: int | None) -> str:
    if deg is None:
        return ""
    return _DIRS[int((deg + 22.5) % 360 // 45)]


def _ago_text(ts: float | None) -> str:
    if not ts:
        return "从未上报"
    sec = max(0, time.time() - ts)
    if sec < 90:
        return "刚刚"
    if sec < 3600:
        return f"{int(sec // 60)}分钟前"
    if sec < 86400:
        return f"{sec / 3600:.1f}小时前"
    return f"{sec / 86400:.1f}天前"


def _dist_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (lat1, lon1, lat2, lon2))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371000 * 2 * math.asin(math.sqrt(h))


def _norm_time(text: str, default: datetime) -> str:
    """接受 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS',空则用默认值。"""
    text = (text or "").strip()
    if not text:
        return default.strftime("%Y-%m-%d %H:%M:%S")
    if len(text) == 10:
        return f"{text} 00:00:00"
    return text


def _today_start() -> datetime:
    now = datetime.now()
    return now.replace(hour=0, minute=0, second=0, microsecond=0)


def _device_brief(d: dict[str, Any]) -> str:
    state = "在线" if d.get("online") else f"离线(最后上报 {_ago_text(d.get('last_seen'))})"
    line = f"设备 {d['device_id']}【{d['_hw']}】{state}"
    if d.get("gps_time") and d.get("point_count"):
        spd = d.get("speed") or 0
        line += f",最新定位 {d['gps_time']},速度 {spd:.0f}km/h"
    return line


# ──────────────────────────────────────────────────────────────
# MCP 工具
# ──────────────────────────────────────────────────────────────

mcp = FastMCP(
    name="waimai-tracker",
    instructions=INSTRUCTIONS,
    stateless_http=True,
    # 内网服务,经容器名/内网 IP 访问,关闭 Host 校验(否则非 localhost 会 421)
    transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
)


@mcp.tool(
    name="list_vehicles",
    description=(
        "查询外卖平台所有车辆/设备:硬件类型、在线状态、最近上报时间、当前速度。"
        "用户问'有几辆车/设备在线吗/车队情况'时用这个;后续查询需要的设备号也从这里拿。"
    ),
)
def list_vehicles() -> str:
    devices = _visible_devices()
    if not devices:
        return "平台当前没有任何设备接入。"
    lines = [f"共 {len(devices)} 台设备,在线 {sum(1 for d in devices if d.get('online'))} 台:"]
    lines += [_device_brief(d) for d in devices]
    return "\n".join(lines)


@mcp.tool(
    name="get_vehicle_location",
    description=(
        "查询某辆车的当前位置:街道地址、速度、行驶方向、定位时间、在线状态。"
        "用户问'车在哪/骑手到哪了'时用这个。device_id 不知道就先调 list_vehicles。"
    ),
)
def get_vehicle_location(
    device_id: Annotated[str, Field(description=_DEVICE_ARG_DESC)] = "",
) -> str:
    device_id = _effective_id(device_id)
    if not device_id:
        return "请提供设备号,或先用 list_vehicles 查设备列表。"
    d = _find_device(device_id)
    if d is None:
        return f"没有找到设备 {device_id},可先用 list_vehicles 查设备列表。"
    try:
        p = _get(d["_api"], f"/api/devices/{d['device_id']}/latest")
    except Exception:
        return f"设备 {device_id} 还没有轨迹数据。"
    state = "在线" if d.get("online") else f"已离线,最后上报 {_ago_text(d.get('last_seen'))}"
    if not p.get("located"):
        return f"设备 {device_id}【{d['_hw']}】{state}。最新一包 GPS 未定位(可能在室内),无法给出位置。"
    addr = _rev_geocode(p["lat"], p["lon"])
    spd = p.get("speed") or 0
    moving = f"正以 {spd:.0f}km/h 向{_dir_text(p.get('direction'))}行驶" if spd >= 3 else "处于静止"
    return (
        f"设备 {device_id}【{d['_hw']}】{state}。"
        f"位置:{addr};{moving};定位时间 {p.get('gps_time')}。"
    )


@mcp.tool(
    name="get_track_summary",
    description=(
        "统计某辆车一段时间的行驶摘要:总里程、移动时长、平均/最高速度、起点和终点地址。"
        "用户问'今天跑了多少公里/上午骑了多久'时用这个。时间不填默认统计今天。"
    ),
)
def get_track_summary(
    device_id: Annotated[str, Field(description=_DEVICE_ARG_DESC)] = "",
    start_time: Annotated[str, Field(description="开始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS,默认今天 0 点")] = "",
    end_time: Annotated[str, Field(description="结束时间,默认现在")] = "",
) -> str:
    device_id = _effective_id(device_id)
    if not device_id:
        return "请提供设备号,或先用 list_vehicles 查设备列表。"
    d = _find_device(device_id)
    if d is None:
        return f"没有找到设备 {device_id},可先用 list_vehicles 查设备列表。"
    start = _norm_time(start_time, _today_start())
    end = _norm_time(end_time, datetime.now())
    try:
        data = _get(d["_api"], f"/api/devices/{d['device_id']}/track",
                    start=start, end=end, limit=50000)
    except Exception as exc:
        return f"轨迹查询失败:{exc}"
    pts = data.get("points", [])
    if len(pts) < 2:
        return f"设备 {device_id} 在 {start} ~ {end} 没有有效移动轨迹(可能一直静止或未开机)。"

    dist = 0.0
    move_sec = 0.0
    top_speed = 0.0
    for a, b in zip(pts, pts[1:]):
        dist += _dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
        dt = b["server_ts"] - a["server_ts"]
        if 0 < dt <= 120:
            move_sec += dt
    for p in pts:
        top_speed = max(top_speed, p.get("speed") or 0)
    km = dist / 1000
    hours = move_sec / 3600
    avg = km / hours if hours > 0.01 else 0
    start_addr = _rev_geocode(pts[0]["lat"], pts[0]["lon"])
    end_addr = _rev_geocode(pts[-1]["lat"], pts[-1]["lon"])
    return (
        f"设备 {device_id} 在 {pts[0]['gps_time']} ~ {pts[-1]['gps_time']}:"
        f"行驶约 {km:.1f} 公里,移动时长约 {move_sec / 60:.0f} 分钟,"
        f"平均 {avg:.0f}km/h,最高 {top_speed:.0f}km/h。"
        f"起点:{start_addr};终点:{end_addr}。"
    )


@mcp.tool(
    name="list_vehicle_events",
    description=(
        "查询安全事件记录:摔车、急刹车、颠簸路段、长时间停驻。"
        "用户问'今天有没有摔车/急刹了几次/有什么异常'时用这个。"
        "device_id 不填=查所有设备;时间不填默认今天。"
    ),
)
def list_vehicle_events(
    device_id: Annotated[str, Field(description=_DEVICE_ARG_DESC + ";留空查全部设备")] = "",
    start_time: Annotated[str, Field(description="开始时间 YYYY-MM-DD 或 YYYY-MM-DD HH:MM:SS,默认今天 0 点")] = "",
    end_time: Annotated[str, Field(description="结束时间,默认现在")] = "",
) -> str:
    device_id = _effective_id(device_id)
    start = _norm_time(start_time, _today_start())
    end = _norm_time(end_time, datetime.now())
    targets = _all_devices()
    if device_id:
        targets = [d for d in targets if d["device_id"] == device_id]
        if not targets:
            return f"没有找到设备 {device_id},可先用 list_vehicles 查设备列表。"

    all_events: list[tuple[dict[str, Any], dict[str, Any]]] = []
    for d in targets:
        try:
            data = _get(d["_api"], f"/api/devices/{d['device_id']}/events",
                        start=start, end=end, limit=100)
            all_events += [(d, e) for e in data.get("events", [])]
        except Exception as exc:
            logger.warning("事件查询失败 %s: %s", d["device_id"], exc)
    if not all_events:
        scope = f"设备 {device_id}" if device_id.strip() else "所有设备"
        return f"{scope}在 {start} ~ {end} 没有安全事件记录,一切正常。"

    all_events.sort(key=lambda x: x[1].get("start_time") or "", reverse=True)
    counter: dict[str, int] = {}
    for _, e in all_events:
        name = EVENT_NAMES.get(e["type"], e["type"])
        counter[name] = counter.get(name, 0) + 1
    summary = "、".join(f"{n} {c} 次" for n, c in counter.items())

    lines = [f"{start[:16]} 以来共 {len(all_events)} 条事件:{summary}。明细(最新在前):"]
    for d, e in all_events[:10]:
        name = EVENT_NAMES.get(e["type"], e["type"])
        det = e.get("detail") or {}
        extra = ""
        if e["type"] in ("fall", "fall_suspect") and det.get("direction"):
            extra = f",向{det['direction']}侧倒"
        elif e["type"] == "hard_brake" and det.get("from_kmh") is not None:
            extra = f",从 {det['from_kmh']:.0f} 刹到 {det['to_kmh']:.0f}km/h"
        elif e["type"] == "stop_long" and det.get("duration_s"):
            extra = f",停了 {det['duration_s'] // 60} 分钟"
        lines.append(f"{e['start_time']} 设备 {d['device_id']}:{name}{extra}")
    if len(all_events) > 10:
        lines.append(f"(仅列出最近 10 条,其余 {len(all_events) - 10} 条略)")
    return "\n".join(lines)


@mcp.tool(
    name="get_fleet_overview",
    description=(
        "车队今日总览:每台设备的在线状态、今日行驶里程、今日各类安全事件次数。"
        "用户问'今天车队整体情况/都正常吗'时用这个,一次调用拿到全貌。"
    ),
)
def get_fleet_overview() -> str:
    devices = _visible_devices()
    if not devices:
        return "平台当前没有任何设备接入。"
    start = _today_start().strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    lines = [f"今日车队总览(共 {len(devices)} 台,在线 {sum(1 for d in devices if d.get('online'))} 台):"]
    for d in devices:
        # 里程
        km_text = "无移动"
        try:
            data = _get(d["_api"], f"/api/devices/{d['device_id']}/track",
                        start=start, end=end, limit=50000)
            pts = data.get("points", [])
            if len(pts) >= 2:
                dist = sum(
                    _dist_m(a["lat"], a["lon"], b["lat"], b["lon"])
                    for a, b in zip(pts, pts[1:])
                )
                km_text = f"行驶 {dist / 1000:.1f}km"
        except Exception:
            km_text = "里程查询失败"
        # 事件
        ev_text = "无事件"
        try:
            evs = _get(d["_api"], f"/api/devices/{d['device_id']}/events",
                       start=start, end=end, limit=200).get("events", [])
            if evs:
                counter: dict[str, int] = {}
                for e in evs:
                    name = EVENT_NAMES.get(e["type"], e["type"])
                    counter[name] = counter.get(name, 0) + 1
                ev_text = "、".join(f"{n}{c}次" for n, c in counter.items())
        except Exception:
            ev_text = "事件查询失败"
        state = "在线" if d.get("online") else "离线"
        lines.append(f"设备 {d['device_id']}【{d['_hw']}】{state},今日{km_text},{ev_text}")
    return "\n".join(lines)


app = mcp.streamable_http_app()

if __name__ == "__main__":
    logger.info("外卖平台 MCP 查询服务启动: 端口 %s, 上游 %s / %s", MCP_PORT, JT808_API, MQTT_API)
    uvicorn.run(app, host="0.0.0.0", port=MCP_PORT, log_level="info")
