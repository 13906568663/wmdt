"""外卖平台·车辆数据 MCP 查询服务(独立进程)。

把 JT808 / MQTT 两个轨迹实例的 REST API 聚合成一个 MCP 接入点,
暴露给 AI 平台(agent-flow)做语音查询:车在哪、今天跑了多少、
有没有摔车/急刹等。所有工具返回简短中文文本,方便大模型直接口播。

环境变量:
    TRACKER_JT808_API   JT808 实例地址,默认 http://wmdt:18209
    TRACKER_MQTT_API    MQTT 实例地址,默认 http://wmdt-mqtt:18209
    MCP_PORT            监听端口,默认 18210
    BAIDU_SERVER_AK     百度地图服务端 AK(逆地理门牌级 + POI 检索 + 骑行路线);
                        留空则逆地理走 OSM、路线规划不可用
    BAIDU_REGION        POI 检索限定城市,默认 深圳

启动:python mcp_server.py  →  MCP 接入点 http://<host>:18210/mcp(streamable-http)
"""

from __future__ import annotations

import logging
import math
import os
import re
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
# 百度地图服务端 AK:有则逆地理走百度(门牌级)、启用 plan_route 路线规划;无则退回 OSM。
BAIDU_AK = os.getenv("BAIDU_SERVER_AK", "").strip()
BAIDU_REGION = os.getenv("BAIDU_REGION", "深圳").strip()

_DEVICE_ARG_DESC = (
    f"设备号;系统已锁定为 {FIXED_DEVICE},不填即可"
    if FIXED_DEVICE
    else "设备号;不填自动选当前活跃(最近上报)的设备,一般无需填写,不要向用户询问设备号"
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
    "安全事件记录;并支持从车辆当前位置出发的骑行路线规划(距离/耗时/转弯指引)"
    "和周边地点搜索(美食/餐馆/药店等,带距离评分人均)。"
    "设备号即车辆标识,用户说'车'、'骑手'、'设备'都指它。"
)

if FIXED_DEVICE:
    INSTRUCTIONS += (
        f" 本平台当前只服务一台车(设备号 {FIXED_DEVICE}),所有查询默认就是这台,"
        "无需向用户确认或让用户选择设备,device_id 一律不用填。"
    )
else:
    INSTRUCTIONS += (
        " 查询默认自动跟踪当前活跃(最近上报)的设备,device_id 一律不用填,"
        "严禁向用户反问'要查哪台设备'。"
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


def _pick_active_device() -> str:
    """自动选当前活跃设备:在线里最近上报的;都离线则取最近上报的。"""
    devices = _all_devices()
    if not devices:
        return ""
    online = [d for d in devices if d.get("online")]
    pool = online or devices
    return max(pool, key=lambda d: d.get("last_seen") or 0)["device_id"]


def _effective_id(device_id: str) -> str:
    """锁定设备 > 显式入参 > 自动选活跃设备。演示时无需任何人指定设备。"""
    if FIXED_DEVICE:
        return FIXED_DEVICE
    if (device_id or "").strip():
        return device_id.strip()
    return _pick_active_device()


def _last_located_point(api: str, device_id: str, minutes: int = 30) -> dict[str, Any] | None:
    """回退查最近 minutes 分钟内最后一个有效定位点(用 all=1 取原始点,绕过漂移抑制)。

    设备 GPS 常断续(最新一包可能恰好未定位),定位查询不该因此直接放弃。
    """
    start = (datetime.now() - timedelta(minutes=minutes)).strftime("%Y-%m-%d %H:%M:%S")
    end = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        data = _get(api, f"/api/devices/{device_id}/track", all=1, start=start, end=end, limit=50000)
    except Exception:
        return None
    located = [p for p in data.get("points", []) if p.get("located")]
    return located[-1] if located else None


# ──────────────────────────────────────────────────────────────
# 逆地理:百度优先(门牌级,需 BAIDU_SERVER_AK),OSM Nominatim 兜底
# ──────────────────────────────────────────────────────────────

_geo_cache: dict[tuple[float, float], str] = {}
_geo_lock = threading.Lock()
_geo_last_call = 0.0


def _rev_geocode_baidu(lat: float, lon: float) -> str:
    """百度逆地理(直接吃 WGS84),返回门牌级地址;失败返回空串。"""
    r = _http.get(
        "https://api.map.baidu.com/reverse_geocoding/v3/",
        params={"ak": BAIDU_AK, "output": "json", "coordtype": "wgs84ll",
                "location": f"{lat},{lon}"},
        timeout=8,
    )
    d = r.json()
    if d.get("status") != 0:
        logger.warning("百度逆地理失败 status=%s %s", d.get("status"), d.get("message"))
        return ""
    res = d.get("result") or {}
    text = res.get("formatted_address") or ""
    # 去掉冗长的省市前缀,语音播报更顺
    return text.removeprefix("广东省").removeprefix("深圳市")


def _rev_geocode_osm(lat: float, lon: float) -> str:
    """OSM Nominatim 逆地理(免费兜底,社区级);失败返回空串。"""
    global _geo_last_call
    wait = _geo_last_call + 1.1 - time.time()
    if wait > 0:
        time.sleep(wait)
    _geo_last_call = time.time()
    r = _http.get(
        "https://nominatim.openstreetmap.org/reverse",
        params={"lat": lat, "lon": lon, "format": "jsonv2",
                "accept-language": "zh-CN", "zoom": 17},
        headers={"User-Agent": "waimai-tracker-mcp/1.0"},
        timeout=8,
    )
    a = r.json().get("address", {})
    parts = [a.get(k, "") for k in ("city", "suburb", "quarter", "road")]
    return "".join(p for p in parts if p) or r.json().get("display_name", "")


def _rev_geocode(lat: float, lon: float) -> str:
    """WGS-84 坐标 → 简短中文地址;失败时回退为坐标文本。"""
    if not lat and not lon:
        return "未定位"
    key = (round(lat, 3), round(lon, 3))  # ~100m 网格缓存
    with _geo_lock:
        if key in _geo_cache:
            return _geo_cache[key]
    text = ""
    if BAIDU_AK:
        try:
            text = _rev_geocode_baidu(lat, lon)
        except Exception:
            logger.exception("百度逆地理异常")
    if not text:
        try:
            text = _rev_geocode_osm(lat, lon)
        except Exception:
            pass
    text = text or f"北纬{lat:.4f} 东经{lon:.4f}"
    with _geo_lock:
        _geo_cache[key] = text
    return text


_BD_X_PI = math.pi * 3000.0 / 180.0


def _bd09_to_wgs84(lon_bd: float, lat_bd: float) -> tuple[float, float]:
    """百度 BD09 → WGS-84(近似逆变换,街道级精度足够)。

    事件明细里只存了 BD09 坐标(lat_bd/lon_bd),而 OSM 逆地理要 WGS-84,
    这里 BD09→GCJ02→WGS84 两步还原。
    """
    # BD09 -> GCJ02
    x, y = lon_bd - 0.0065, lat_bd - 0.006
    z = math.sqrt(x * x + y * y) - 0.00002 * math.sin(y * _BD_X_PI)
    theta = math.atan2(y, x) - 0.000003 * math.cos(x * _BD_X_PI)
    glon, glat = z * math.cos(theta), z * math.sin(theta)
    # GCJ02 -> WGS84(粗略反向:算出偏移后相减)
    a, ee = 6378245.0, 0.00669342162296594323
    dlat = (
        -100.0 + 2.0 * (glon - 105.0) + 3.0 * (glat - 35.0)
        + 0.2 * (glat - 35.0) ** 2 + 0.1 * (glon - 105.0) * (glat - 35.0)
        + 0.2 * math.sqrt(abs(glon - 105.0))
        + (20.0 * math.sin(6.0 * (glon - 105.0) * math.pi) + 20.0 * math.sin(2.0 * (glon - 105.0) * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin((glat - 35.0) * math.pi) + 40.0 * math.sin((glat - 35.0) / 3.0 * math.pi)) * 2.0 / 3.0
        + (160.0 * math.sin((glat - 35.0) / 12.0 * math.pi) + 320 * math.sin((glat - 35.0) * math.pi / 30.0)) * 2.0 / 3.0
    )
    dlon = (
        300.0 + (glon - 105.0) + 2.0 * (glat - 35.0) + 0.1 * (glon - 105.0) ** 2
        + 0.1 * (glon - 105.0) * (glat - 35.0) + 0.1 * math.sqrt(abs(glon - 105.0))
        + (20.0 * math.sin(6.0 * (glon - 105.0) * math.pi) + 20.0 * math.sin(2.0 * (glon - 105.0) * math.pi)) * 2.0 / 3.0
        + (20.0 * math.sin((glon - 105.0) * math.pi) + 40.0 * math.sin((glon - 105.0) / 3.0 * math.pi)) * 2.0 / 3.0
        + (150.0 * math.sin((glon - 105.0) / 12.0 * math.pi) + 300.0 * math.sin((glon - 105.0) / 30.0 * math.pi)) * 2.0 / 3.0
    )
    radlat = glat / 180.0 * math.pi
    magic = 1 - ee * math.sin(radlat) ** 2
    sqrtmagic = math.sqrt(magic)
    dlat = (dlat * 180.0) / ((a * (1 - ee)) / (magic * sqrtmagic) * math.pi)
    dlon = (dlon * 180.0) / (a / sqrtmagic * math.cos(radlat) * math.pi)
    return glon - dlon, glat - dlat


def _event_addr(detail: dict[str, Any]) -> str:
    """从事件明细的 BD09 坐标反查街道地址;无坐标返回空串。"""
    lon_bd, lat_bd = detail.get("lon_bd"), detail.get("lat_bd")
    if not lon_bd or not lat_bd:
        return ""
    lon, lat = _bd09_to_wgs84(float(lon_bd), float(lat_bd))
    return _rev_geocode(lat, lon)


# ──────────────────────────────────────────────────────────────
# 百度 POI 检索 + 骑行路线规划(plan_route 用)
# ──────────────────────────────────────────────────────────────


def _poi_search(query: str) -> dict[str, Any] | None:
    """百度地点检索:目的地名称 → {name, address, lat_bd, lon_bd};找不到返回 None。"""
    r = _http.get(
        "https://api.map.baidu.com/place/v2/search",
        params={"ak": BAIDU_AK, "output": "json", "query": query,
                "region": BAIDU_REGION, "city_limit": "true", "page_size": 1},
        timeout=8,
    )
    d = r.json()
    if d.get("status") != 0 or not d.get("results"):
        return None
    top = d["results"][0]
    loc = top.get("location") or {}
    if not loc.get("lat"):
        return None
    return {"name": top.get("name", query), "address": top.get("address", ""),
            "lat_bd": loc["lat"], "lon_bd": loc["lng"]}


_TAG_RE = re.compile(r"<[^>]+>")


def _poi_nearby(query: str, lat_bd: float, lon_bd: float, radius_m: int = 1500) -> list[dict[str, Any]]:
    """百度周边检索:以 BD09 坐标为圆心搜关键词,返回按距离排序的 POI 列表。"""
    r = _http.get(
        "https://api.map.baidu.com/place/v2/search",
        params={"ak": BAIDU_AK, "output": "json", "query": query,
                "location": f"{lat_bd},{lon_bd}", "radius": radius_m,
                "scope": 2, "filter": "sort_name:distance", "page_size": 8},
        timeout=8,
    )
    d = r.json()
    if d.get("status") != 0:
        logger.warning("百度周边检索失败 status=%s %s", d.get("status"), d.get("message"))
        return []
    out = []
    for item in d.get("results") or []:
        det = item.get("detail_info") or {}
        out.append({
            "name": item.get("name", ""),
            "address": item.get("address", ""),
            "distance_m": det.get("distance"),
            "rating": det.get("overall_rating"),
            "price": det.get("price"),
        })
    return out


def _riding_route(o_lat_bd: float, o_lon_bd: float, d_lat_bd: float, d_lon_bd: float) -> dict[str, Any] | None:
    """百度骑行路线规划(电动车模式,BD09 坐标);失败返回 None。"""
    r = _http.get(
        "https://api.map.baidu.com/direction/v2/riding",
        params={"ak": BAIDU_AK, "riding_type": 1,
                "origin": f"{o_lat_bd},{o_lon_bd}",
                "destination": f"{d_lat_bd},{d_lon_bd}"},
        timeout=12,
    )
    d = r.json()
    routes = (d.get("result") or {}).get("routes") or []
    if d.get("status") != 0 or not routes:
        logger.warning("百度骑行规划失败 status=%s %s", d.get("status"), d.get("message"))
        return None
    route = routes[0]
    steps = []
    for s in route.get("steps", []):
        text = _TAG_RE.sub("", s.get("instructions") or s.get("instruction") or "").strip()
        if text:
            steps.append(text)
    return {"distance_m": route.get("distance", 0), "duration_s": route.get("duration", 0), "steps": steps}


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
    if p.get("located"):
        addr = _rev_geocode(p["lat"], p["lon"])
        spd = p.get("speed") or 0
        moving = f"正以 {spd:.0f}km/h 向{_dir_text(p.get('direction'))}行驶" if spd >= 3 else "处于静止"
        return (
            f"设备 {device_id}【{d['_hw']}】{state}。"
            f"位置:{addr};{moving};定位时间 {p.get('gps_time')}。"
        )
    # 最新一包未定位:GPS 断续很常见,回退到最近 30 分钟内最后一个有效定位点,
    # 别直接说"查不到"——多半几分钟前才刚定位过。
    last = _last_located_point(d["_api"], d["device_id"], minutes=30)
    if last is None:
        return (
            f"设备 {device_id}【{d['_hw']}】{state}。当前 GPS 未定位,"
            "最近半小时也没有有效定位(可能一直在室内或信号遮挡),暂时给不出位置。"
        )
    addr = _rev_geocode(last["lat"], last["lon"])
    ago = _ago_text(last.get("server_ts"))
    return (
        f"设备 {device_id}【{d['_hw']}】{state}。当前 GPS 信号弱、暂未定位;"
        f"最近一次定位在:{addr}(定位时间 {last['gps_time']},约 {ago})。"
    )


@mcp.tool(
    name="plan_route",
    description=(
        "路线规划:从车辆当前位置骑行去某个目的地,返回距离、预计耗时和关键转弯指引。"
        "用户问'去XX怎么走/到XX多远/骑过去要多久'时用这个。"
        "destination 填目的地名称(如'西乡地铁站'、'沃尔玛蛇口店'),不用带城市名。"
    ),
)
def plan_route(
    destination: Annotated[str, Field(description="目的地名称或地址,如 西乡地铁站")],
    device_id: Annotated[str, Field(description=_DEVICE_ARG_DESC)] = "",
) -> str:
    if not BAIDU_AK:
        return "路线规划服务未配置(缺少百度地图 AK),暂时无法使用。"
    destination = (destination or "").strip()
    if not destination:
        return "请告诉我目的地名称,比如'西乡地铁站'。"
    device_id = _effective_id(device_id)
    d = _find_device(device_id) if device_id else None
    if d is None:
        return f"没有找到设备 {device_id},无法确定出发位置。"

    # 起点:最新定位包,未定位则回退最近 30 分钟内的有效定位点
    origin = None
    try:
        p = _get(d["_api"], f"/api/devices/{d['device_id']}/latest")
        if p.get("located"):
            origin = p
    except Exception:
        pass
    if origin is None:
        origin = _last_located_point(d["_api"], d["device_id"], minutes=30)
    if origin is None:
        return "当前查不到车辆位置(GPS 长时间未定位),无法规划路线。"

    poi = _poi_search(destination)
    if poi is None:
        return f"没有找到目的地「{destination}」,换个更具体的名称试试(如带上商圈或街道名)。"

    from tracker.geo import wgs84_to_bd09

    o_lon_bd, o_lat_bd = wgs84_to_bd09(origin["lon"], origin["lat"])
    route = _riding_route(o_lat_bd, o_lon_bd, poi["lat_bd"], poi["lon_bd"])
    if route is None:
        return f"到「{poi['name']}」的骑行路线规划失败,稍后再试。"

    km = route["distance_m"] / 1000
    mins = max(1, round(route["duration_s"] / 60))
    # 语音场景只报前几个关键动作,不逐条念完
    steps = route["steps"][:4]
    step_text = ";".join(steps)
    if len(route["steps"]) > 4:
        step_text += ";之后按导航继续"
    dest_text = poi["name"] + (f"({poi['address']})" if poi.get("address") else "")
    return (
        f"从当前位置到{dest_text}:骑行约 {km:.1f} 公里,预计 {mins} 分钟。"
        f"路线:{step_text}。"
    )


@mcp.tool(
    name="find_nearby",
    description=(
        "搜索车辆当前位置周边的地点:美食/餐馆/小吃/药店/超市/充电站等,"
        "返回名称、距离、评分、人均价格。用户问'附近有什么吃的/哪家评分高/最近的XX在哪'时用这个。"
        "keyword 填要找的东西,如'美食'、'麻辣烫'、'沙县小吃'、'充电站'。"
        "结果是本地实时数据,比联网搜索快且准,周边问题优先用我。"
    ),
)
def find_nearby(
    keyword: Annotated[str, Field(description="搜索关键词,如 美食、麻辣烫、药店")],
    radius_m: Annotated[int, Field(description="搜索半径(米),默认 1500")] = 1500,
    device_id: Annotated[str, Field(description=_DEVICE_ARG_DESC)] = "",
) -> str:
    if not BAIDU_AK:
        return "周边搜索服务未配置(缺少百度地图 AK),暂时无法使用。"
    keyword = (keyword or "").strip() or "美食"
    device_id = _effective_id(device_id)
    d = _find_device(device_id) if device_id else None
    if d is None:
        return f"没有找到设备 {device_id},无法确定搜索中心位置。"

    origin = None
    try:
        p = _get(d["_api"], f"/api/devices/{d['device_id']}/latest")
        if p.get("located"):
            origin = p
    except Exception:
        pass
    if origin is None:
        origin = _last_located_point(d["_api"], d["device_id"], minutes=30)
    if origin is None:
        return "当前查不到车辆位置(GPS 长时间未定位),无法搜索周边。"

    from tracker.geo import wgs84_to_bd09

    lon_bd, lat_bd = wgs84_to_bd09(origin["lon"], origin["lat"])
    pois = _poi_nearby(keyword, lat_bd, lon_bd, radius_m=max(200, min(radius_m, 5000)))
    if not pois:
        return f"附近 {radius_m} 米内没搜到「{keyword}」,可以换个关键词或扩大范围再试。"

    lines = [f"附近的「{keyword}」找到 {len(pois)} 家(按距离从近到远):"]
    for poi in pois:
        seg = poi["name"]
        if poi.get("distance_m") is not None:
            seg += f",{poi['distance_m']}米"
        if poi.get("rating"):
            seg += f",评分{poi['rating']}"
        if poi.get("price"):
            seg += f",人均{float(poi['price']):.0f}元"
        lines.append(seg)
    return "\n".join(lines)


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
        "查询安全事件记录:摔车、急刹车、颠簸路段、长时间停驻,每条都带发生地点(街道地址)。"
        "用户问'今天有没有摔车/在哪摔的/急刹了几次/有什么异常'时用这个。"
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
        addr = _event_addr(det)
        where = f",地点:{addr}" if addr else ""
        lines.append(f"{e['start_time']} 设备 {d['device_id']}:{name}{extra}{where}")
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
