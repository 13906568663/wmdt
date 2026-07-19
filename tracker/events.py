"""事件规则引擎:摔车(含方向)/疑似摔车/超速/急刹/颠簸/长停驻。

展示层(网页事件列表与 MCP 查询)只暴露摔车与超速两类;急刹/颠簸/停驻
仍照常入库留作数据积累,由 storage.list_events 统一过滤。

阈值来源与回代验证见 docs/事件规则分析_20260715夜测.md。设计要点:

- 与协议无关:输入为入库前的轨迹点,JT808 与 MQTT 共用;
- 六轴两组字段哪组是加速度计(重力),由静止自标定自动识别——静止时
  模长恒等于 1g 的那组就是加速度计,同时识别刻度(16384 LSB/g 或 1000 mG/g),
  以兼容固件字段互换与不同量纲;
- 直立基准矢量按设备静止段自标定(设备普遍斜装,不能假设 Z 轴朝上),
  倾角 = 当前重力矢量与直立基准的夹角;
- 摔倒方向 = 倒地"稳态"样本(陀螺最安静的那包)的重力矢量相对直立基准的
  变化分量,轴向语义(x+左/x-右/y-前/y+后)基于当前车型安装方向约定。

状态仅存内存:进程重启后设备静止约 1 分钟即自动重新标定。
"""

from __future__ import annotations

import json
import logging
import math
import os
import statistics
import time
from typing import Any

from .storage import Storage

logger = logging.getLogger("events")

# ── 自标定 ──
GRAVITY_SCALES = (16384.0, 1000.0)  # 1g 候选刻度:原始 LSB(±2g 量程)/ mG
SCALE_TOLERANCE = 0.5               # |log(模长/刻度)| 容差(约 ±65%)
CAL_SAMPLES = 12                    # 连续静止标定样本数(滑窗)
CAL_MAX_SPREAD_DEG = 15.0           # 静止标定样本与基准最大夹角
CAL_KEEP_RATIO = 0.7                # 剔除离群样本后至少保留比例
REF_UPDATE_TILT = 25.0              # 已有基准时仅在近直立状态缓慢更新
# 骑行标定:骑行中车必然直立,用平稳骑行段刷新直立基准。
# 换装/静止标定误锁(设备被手持摆放)后无需长时间静止即可恢复检测。
RIDE_CAL_V_KMH = 5.0
RIDE_CAL_MIN_N = 4                  # 最少样本数
RIDE_CAL_SPAN_S = 15.0              # 样本须覆盖的最短时间跨度(兼容 1s/5s 上报)
RIDE_CAL_MAX_N = 12
RIDE_CAL_WINDOW_S = 60.0
RIDE_CAL_SPREAD_DEG = 25.0          # 骑行样本含转弯/颠簸,容差放宽

# ── 摔车 ──
FALL_TILT = 65.0                    # 确定摔倒倾角(0716 右前方摔车 67° 回代下调)
MID_TILT = 45.0                     # 疑似摔倒倾角下限
RECOVER_TILT = 40.0                 # 恢复倾角
CONFIRM_N = 2                       # 连续包数(确认/恢复)
GYRO_SPIKE = {16384.0: 2500.0, 1000.0: 150.0}  # 陀螺突跳阈值,按加速度刻度配套
SPIKE_MEMORY_S = 20.0               # 尖峰记忆窗口
DROP_MEMORY_S = 12.0                # "速度突然归零"记忆窗口
STOP_DROP_KMH = 3.0                 # 速度突降判定:近窗内曾 ≥ 此值而当前静止
DIR_DELTA_G = 0.30                  # 方向分量阈值(g)
AXIS_LABELS = {"x+": "左", "x-": "右", "y+": "后", "y-": "前", "z+": "翻正", "z-": "翻覆"}
REMOUNT_RIDE_S = 15.0               # 倾斜状态持续骑行超过此时长 → 判为安装方向变化,作废摔车并重新标定

# ── 急刹 ──
BRAKE_DV_KMH = -10.0
BRAKE_MAX_GAP_S = 8.0
BRAKE_END_KMH = 5.0
BRAKE_COOLDOWN_S = 20.0
# 陀螺辅助急刹:低速急刹 GPS 速度差不够(采样稀释),但刹车点头在陀螺上有尖峰。
# 条件:当前静止 + 近12s内最高速≥阈值(过滤蠕行进红灯/起步顿挫) + 近窗陀螺尖峰。
# 阈值由 0715/0716 两晚全部骤停点回代标定。
BRAKE_ASSIST_V_KMH = 7.5
BRAKE_ASSIST_GYRO_S = 8.0
BRAKE_ASSIST_GYRO = {16384.0: 1000.0, 1000.0: 60.0}  # 按加速度刻度配套的陀螺阈值

# ── 颠簸 ──
BUMP_WINDOW_S = 75.0
BUMP_MIN_SAMPLES = 8
BUMP_MIN_V_KMH = 3.0
BUMP_STD_G = 0.22
BUMP_MERGE_GAP_S = 90.0

# ── 停驻 ──
STOP_V_KMH = 2.0                    # 低于视为静止
GO_V_KMH = 4.0                      # 高于视为恢复移动(迟滞)
PARK_MIN_S = 300.0                  # 仅记录 ≥5 分钟的长停驻(等餐/驻车),红绿灯等短停不入库

# ── 超速 ──
SPEED_LIMIT_KMH = float(os.getenv("TRACKER_SPEED_LIMIT_KMH", "25"))  # 电动车国标限速
SPEED_CONFIRM_N = 3                 # 连续超限包数才开事件(滤 GPS 速度毛刺)
SPEED_END_KMH = max(5.0, SPEED_LIMIT_KMH - 3.0)  # 迟滞:回落到此速以下才算结束


def _mag(v: tuple[float, float, float]) -> float:
    return math.sqrt(v[0] ** 2 + v[1] ** 2 + v[2] ** 2)


def _angle_deg(a: tuple[float, float, float], b: tuple[float, float, float]) -> float:
    na, nb = _mag(a), _mag(b)
    if na == 0 or nb == 0:
        return 0.0
    c = (a[0] * b[0] + a[1] * b[1] + a[2] * b[2]) / (na * nb)
    return math.degrees(math.acos(max(-1.0, min(1.0, c))))


class _DevState:
    def __init__(self) -> None:
        self.last_t = 0.0
        self.dt = 0.0
        self.last_pos: tuple[float, float] | None = None  # (lon_bd, lat_bd)
        # 标定
        self.cal_buf: list[tuple[float, ...]] = []
        self.ride_buf: list[tuple[float, tuple[float, ...]]] = []  # (t, 六轴) 骑行样本
        self.accel_idx: int | None = None   # 哪组是加速度计:0=前六字节组 1=后六字节组
        self.scale = 0.0
        self.ref: tuple[float, float, float] | None = None  # 直立基准(g)
        # 摔车
        self.spikes: list[tuple[float, float]] = []     # (t, |G|)
        self.speeds: list[tuple[float, float]] = []     # (t, v)
        self.run_mid = 0
        self.run_high = 0
        self.run_start = ""
        self.steady: tuple[float, tuple[float, float, float]] | None = None  # (|G|, a_g)
        self.tilt_max = 0.0
        self.gyro_peak = 0.0
        self.fall_id: int | None = None
        self.fall_confirmed = False
        self.recover = 0
        self.last_fall_t = 0.0
        self.tilt_ride_s = 0.0      # 倾斜状态下累计骑行时长(识别重新安装)
        # 急刹
        self.prev_v: tuple[float, float] | None = None  # (t, v)
        self.last_brake_t = 0.0
        # 颠簸
        self.bump_buf: list[tuple[float, float]] = []   # (t, |A|g)
        self.bump_id: int | None = None
        self.bump_last_t = 0.0
        # 停驻
        self.stop_since: float | None = None
        self.stop_start_time = ""
        self.stop_id: int | None = None
        self.stop_saw_fall = False
        # 超速
        self.over_run = 0
        self.speed_id: int | None = None
        self.speed_start = ""
        self.speed_max = 0.0


class EventDetector:
    """每设备独立状态机;process() 在每个轨迹点入库后调用。"""

    def __init__(self, storage: Storage) -> None:
        self.storage = storage
        self._states: dict[str, _DevState] = {}

    # ── 入口 ───────────────────────────────────────────

    def process(self, device_id: str, point: dict[str, Any],
                lon_bd: float | None = None, lat_bd: float | None = None) -> None:
        try:
            self._process(device_id, point, lon_bd, lat_bd)
        except Exception:  # 规则引擎异常不能影响数据接入
            logger.exception("事件检测异常 device=%s", device_id)

    def _process(self, device_id: str, point: dict[str, Any],
                 lon_bd: float | None, lat_bd: float | None) -> None:
        if device_id not in self._states:
            # 进程重启后内存状态丢失,先闭合库里遗留的未结束事件,避免永远"进行中"
            self.storage.close_dangling_events(device_id)
        st = self._states.setdefault(device_id, _DevState())
        t = self._parse_time(point.get("gps_time") or "")
        if t <= st.last_t:  # 重复/乱序包
            return
        st.dt = t - st.last_t if st.last_t else 0.0
        st.last_t = t
        v = float(point.get("speed") or 0)
        if point.get("located") and lon_bd and lat_bd:
            st.last_pos = (lon_bd, lat_bd)
        st.speeds.append((t, v))
        st.speeds = [x for x in st.speeds if t - x[0] <= DROP_MEMORY_S]

        triplets = self._six_axis(point)
        a_g: tuple[float, float, float] | None = None
        gmag: float | None = None
        if triplets:
            self._calibrate(st, triplets, v, t)
            if st.accel_idx is not None:
                acc_raw = triplets[st.accel_idx]
                a_g = (acc_raw[0] / st.scale, acc_raw[1] / st.scale, acc_raw[2] / st.scale)
                gmag = _mag(triplets[1 - st.accel_idx])
                st.spikes.append((t, gmag))
                st.spikes = [x for x in st.spikes if t - x[0] <= SPIKE_MEMORY_S]

        gps_time = point.get("gps_time") or ""
        tilt: float | None = None
        if a_g is not None and st.ref is not None:
            tilt = _angle_deg(a_g, st.ref)
            self._fall_rule(device_id, st, t, gps_time, v, a_g, gmag or 0.0)
            self._bump_rule(device_id, st, t, gps_time, v, a_g)
        self._brake_rule(device_id, st, t, gps_time, v, tilt)
        self._stop_rule(device_id, st, t, gps_time, v)
        self._speed_rule(device_id, st, t, gps_time, v)
        st.prev_v = (t, v)

    # ── 标定 ───────────────────────────────────────────

    def _calibrate(self, st: _DevState, triplets: list[tuple[float, ...]],
                   v: float, t: float) -> None:
        six = tuple(triplets[0]) + tuple(triplets[1])
        if v >= STOP_V_KMH:
            st.cal_buf = []
            if v >= RIDE_CAL_V_KMH:
                self._ride_cal(st, six, t)
            return
        if st.ref is not None and st.accel_idx is not None:
            # 已标定:仅近直立时缓慢跟随(防止倒地/搬动把基准带偏)
            a = triplets[st.accel_idx]
            a_g = (a[0] / st.scale, a[1] / st.scale, a[2] / st.scale)
            if _angle_deg(a_g, st.ref) >= REF_UPDATE_TILT:
                st.cal_buf = []
                return
        st.cal_buf.append(six)
        if len(st.cal_buf) >= CAL_SAMPLES:
            self._finish_cal(st, st.cal_buf[-CAL_SAMPLES:], CAL_MAX_SPREAD_DEG, "静止")
            # 滑动窗口:无论成败保留最近样本,下一包继续尝试
            st.cal_buf = st.cal_buf[-(CAL_SAMPLES - 1):]

    def _ride_cal(self, st: _DevState, six: tuple[float, ...], t: float) -> None:
        """骑行标定:骑行=直立,平稳骑行段的重力中位矢量就是直立基准。"""
        st.ride_buf.append((t, six))
        st.ride_buf = [x for x in st.ride_buf if t - x[0] <= RIDE_CAL_WINDOW_S][-RIDE_CAL_MAX_N:]
        if (len(st.ride_buf) < RIDE_CAL_MIN_N
                or t - st.ride_buf[0][0] < RIDE_CAL_SPAN_S):
            return
        # 摔车状态机活跃时禁止刷新(倾斜骑行=换装场景,交由 void 仲裁清基准后立即恢复)
        if st.ref is not None and (st.fall_id is not None or st.run_mid > 0):
            return
        if self._finish_cal(st, [s for _, s in st.ride_buf], RIDE_CAL_SPREAD_DEG, "骑行"):
            st.ride_buf = []

    def _finish_cal(self, st: _DevState, samples: list[tuple[float, ...]],
                    spread_deg: float, source: str) -> bool:
        med = [statistics.median(s[i] for s in samples) for i in range(6)]
        cands = [tuple(med[0:3]), tuple(med[3:6])]
        best: tuple[float, int, float] | None = None  # (score, idx, scale)
        for idx, c in enumerate(cands):
            m = _mag(c)  # type: ignore[arg-type]
            if m <= 0:
                continue
            for scale in GRAVITY_SCALES:
                score = abs(math.log(m / scale))
                if best is None or score < best[0]:
                    best = (score, idx, scale)
        if best is None or best[0] > SCALE_TOLERANCE:
            return False
        _, idx, scale = best
        ref = tuple(x / scale for x in cands[idx])
        # 剔除离群样本(转弯/颠簸/瞬时扰动),保留量不足说明姿态不稳,放弃本窗
        kept = [s for s in samples
                if _angle_deg((s[idx * 3] / scale, s[idx * 3 + 1] / scale,
                               s[idx * 3 + 2] / scale), ref) <= spread_deg]  # type: ignore[arg-type]
        if len(kept) < max(4, int(len(samples) * CAL_KEEP_RATIO)):
            return False
        med2 = [statistics.median(s[i] for s in kept) for i in range(6)]
        ref = tuple(med2[idx * 3 + j] / scale for j in range(3))
        changed = st.accel_idx != idx or st.ref is None
        st.accel_idx, st.scale, st.ref = idx, scale, ref  # type: ignore[assignment]
        if changed:
            logger.info("六轴标定完成(%s): 加速度计=第%s组 刻度=%s/g 基准=%s",
                        source, idx + 1, int(scale), tuple(round(x, 2) for x in ref))
        return True

    # ── 摔车 ───────────────────────────────────────────

    def _fall_rule(self, device_id: str, st: _DevState, t: float, gps_time: str,
                   v: float, a_g: tuple[float, float, float], gmag: float) -> None:
        assert st.ref is not None
        tilt = _angle_deg(a_g, st.ref)
        spike_th = GYRO_SPIKE.get(st.scale, GYRO_SPIKE[1000.0])
        if tilt >= MID_TILT:
            st.run_mid += 1
            st.run_high = st.run_high + 1 if tilt >= FALL_TILT else 0
            st.recover = 0
            if not st.run_start:
                st.run_start = gps_time
            st.tilt_max = max(st.tilt_max, tilt)
            st.gyro_peak = max(st.gyro_peak, max((g for _, g in st.spikes), default=0.0))
            if st.steady is None or gmag < st.steady[0]:
                st.steady = (gmag, a_g)

            confirmed = st.run_high >= CONFIRM_N
            spike_recent = any(g >= spike_th for _, g in st.spikes)
            speed_drop = v < STOP_V_KMH and any(vv >= STOP_DROP_KMH for _, vv in st.speeds[:-1])
            suspect = st.run_mid >= CONFIRM_N and (spike_recent or speed_drop)

            # 倾斜状态下仍持续骑行 → 不是摔倒,是设备被换了安装方向:
            # 作废本次摔车,清基准触发重新标定(下一段静止自动完成)
            st.tilt_ride_s = st.tilt_ride_s + st.dt if v >= GO_V_KMH else 0.0
            if st.tilt_ride_s >= REMOUNT_RIDE_S:
                if st.fall_id is not None:
                    self.storage.update_event(st.fall_id, etype="void", end_time=gps_time,
                                              detail={"reason": "倾斜状态持续骑行,判为安装方向变化"})
                logger.info("检测到安装方向变化 device=%s,重新标定", device_id)
                self._reset_fall(st)
                # 清基准触发重标定;骑行缓冲保留,下一包即可用新姿态恢复
                st.ref = None
                st.accel_idx = None
                st.cal_buf = []
                return

            if st.fall_id is None and (confirmed or suspect):
                etype = "fall" if confirmed else "fall_suspect"
                st.fall_id = self._insert(device_id, etype, st.run_start, None, st, t)
                st.fall_confirmed = confirmed
                st.last_fall_t = t
                st.stop_saw_fall = True
            elif st.fall_id is not None and confirmed and not st.fall_confirmed:
                self.storage.update_event(st.fall_id, etype="fall")
                st.fall_confirmed = True
            if st.fall_id is not None:
                st.last_fall_t = t
        else:
            if st.fall_id is not None:
                if tilt < RECOVER_TILT:
                    st.recover += 1
                    if st.recover >= CONFIRM_N:
                        self.storage.update_event(
                            st.fall_id, end_time=gps_time,
                            detail=self._fall_detail(st))
                        self._reset_fall(st)
                # RECOVER_TILT~MID_TILT 之间保持现状
            elif tilt < RECOVER_TILT:
                self._reset_fall(st)

    def _fall_detail(self, st: _DevState) -> dict[str, Any]:
        detail: dict[str, Any] = {
            "tilt_max": round(st.tilt_max, 1),
            "gyro_peak": round(st.gyro_peak, 1),
        }
        if st.steady is not None and st.ref is not None:
            a = st.steady[1]
            # 倾倒指向 = 倒地重力矢量中垂直于直立基准的分量。
            # 直立时重力压在的轴倒地后分量必然剧减,直接做差会误判为"前",
            # 投影剔除基准方向后,剩余分量才是车往哪边倒。
            nr = _mag(st.ref)
            r = (st.ref[0] / nr, st.ref[1] / nr, st.ref[2] / nr)
            dot = a[0] * r[0] + a[1] * r[1] + a[2] * r[2]
            perp = (a[0] - dot * r[0], a[1] - dot * r[1], a[2] - dot * r[2])
            # 本车安装约定:x+ 左 / x- 右(经 0715 夜测 4 次摔车回代验证)
            if perp[0] >= DIR_DELTA_G:
                detail["direction"] = "左"
            elif perp[0] <= -DIR_DELTA_G:
                detail["direction"] = "右"
            else:
                detail["direction"] = "不明"
            detail["gravity_g"] = [round(x, 2) for x in a]
            detail["topple_g"] = [round(x, 2) for x in perp]
        return detail

    def _reset_fall(self, st: _DevState) -> None:
        st.run_mid = st.run_high = st.recover = 0
        st.run_start = ""
        st.tilt_ride_s = 0.0
        st.steady = None
        st.tilt_max = st.gyro_peak = 0.0
        st.fall_id = None
        st.fall_confirmed = False

    # ── 急刹 ───────────────────────────────────────────

    def _brake_rule(self, device_id: str, st: _DevState, t: float, gps_time: str,
                    v: float, tilt: float | None) -> None:
        prev = st.prev_v
        if prev is None:
            return
        pt, pv = prev
        dt = t - pt
        if dt <= 0 or dt > BRAKE_MAX_GAP_S:
            return
        fall_recent = st.fall_id is not None or (t - st.last_fall_t) < 10
        if fall_recent or t - st.last_brake_t <= BRAKE_COOLDOWN_S:
            return
        dv = v - pv
        strong = dv <= BRAKE_DV_KMH and v < BRAKE_END_KMH
        # 陀螺辅助:低速急刹 GPS 速度差常不足 10 km/h,用"骤停+刹车点头尖峰"补判。
        # 姿态须直立(排除摔倒骤停),前一包在动(排除停稳后的原地扰动)。
        assist = False
        if (not strong and v < STOP_V_KMH and pv >= STOP_V_KMH and st.scale
                and tilt is not None and tilt < MID_TILT):
            recent_v = [vv for tt, vv in st.speeds if tt < t]
            gy_th = BRAKE_ASSIST_GYRO.get(st.scale, 60.0)
            gyro_hit = any(g >= gy_th for tt, g in st.spikes if t - tt <= BRAKE_ASSIST_GYRO_S)
            assist = bool(recent_v) and max(recent_v) >= BRAKE_ASSIST_V_KMH and gyro_hit
        if strong or assist:
            st.last_brake_t = t
            from_v = pv if strong else max(vv for tt, vv in st.speeds if tt < t)
            detail = {"from_kmh": round(from_v, 1), "to_kmh": round(v, 1), "dt_s": round(dt, 1)}
            if assist:
                detail["assist"] = 1
            self._insert(device_id, "hard_brake", gps_time, gps_time, st, t, detail=detail)

    # ── 颠簸 ───────────────────────────────────────────

    def _bump_rule(self, device_id: str, st: _DevState, t: float, gps_time: str,
                   v: float, a_g: tuple[float, float, float]) -> None:
        if st.fall_id is not None:
            return
        if v >= BUMP_MIN_V_KMH:
            st.bump_buf.append((t, _mag(a_g)))
        st.bump_buf = [x for x in st.bump_buf if t - x[0] <= BUMP_WINDOW_S]
        if st.bump_id is not None and t - st.bump_last_t > BUMP_MERGE_GAP_S:
            st.bump_id = None
        if len(st.bump_buf) < BUMP_MIN_SAMPLES:
            return
        std = statistics.pstdev(m for _, m in st.bump_buf)
        if std < BUMP_STD_G:
            return
        st.bump_last_t = t
        if st.bump_id is None:
            st.bump_id = self._insert(device_id, "bump", gps_time, gps_time, st, t,
                                      detail={"std_g": round(std, 2)})
        else:
            self.storage.update_event(st.bump_id, end_time=gps_time,
                                      detail={"std_g": round(std, 2)})

    # ── 停驻 ───────────────────────────────────────────

    def _stop_rule(self, device_id: str, st: _DevState, t: float, gps_time: str, v: float) -> None:
        """只记录 ≥PARK_MIN_S 的长停驻(等餐/驻车),红绿灯等短停不入库。"""
        if st.stop_since is None:
            if v < STOP_V_KMH:
                st.stop_since = t
                st.stop_start_time = gps_time
                st.stop_saw_fall = st.fall_id is not None
            return
        if st.fall_id is not None:
            st.stop_saw_fall = True
        dur = t - st.stop_since
        if v >= GO_V_KMH:
            if st.stop_id is not None:
                self.storage.update_event(st.stop_id, end_time=gps_time,
                                          detail={"duration_s": int(dur)})
            st.stop_since = None
            st.stop_id = None
            st.stop_saw_fall = False
            return
        if dur >= PARK_MIN_S and st.stop_id is None and not st.stop_saw_fall:
            st.stop_id = self._insert(device_id, "stop_long", st.stop_start_time, gps_time,
                                      st, t, detail={"duration_s": int(dur)})
        elif st.stop_id is not None:
            self.storage.update_event(st.stop_id, end_time=gps_time,
                                      detail={"duration_s": int(dur)})

    # ── 超速 ───────────────────────────────────────────

    def _speed_rule(self, device_id: str, st: _DevState, t: float, gps_time: str, v: float) -> None:
        """连续 SPEED_CONFIRM_N 包超过限速开事件;回落到迟滞线以下闭合。"""
        if v >= SPEED_LIMIT_KMH:
            st.over_run += 1
            if st.over_run == 1:
                st.speed_start = gps_time
            st.speed_max = max(st.speed_max, v)
            if st.speed_id is None and st.over_run >= SPEED_CONFIRM_N:
                st.speed_id = self._insert(
                    device_id, "overspeed", st.speed_start, gps_time, st, t,
                    detail={"max_kmh": round(st.speed_max, 1), "limit_kmh": SPEED_LIMIT_KMH})
            elif st.speed_id is not None:
                self.storage.update_event(st.speed_id, end_time=gps_time,
                                          detail={"max_kmh": round(st.speed_max, 1)})
        elif v < SPEED_END_KMH:
            if st.speed_id is not None:
                self.storage.update_event(st.speed_id, end_time=gps_time,
                                          detail={"max_kmh": round(st.speed_max, 1)})
            st.over_run = 0
            st.speed_id = None
            st.speed_max = 0.0
        # SPEED_END_KMH ~ SPEED_LIMIT_KMH 之间维持现状(迟滞区)

    # ── 工具 ───────────────────────────────────────────

    def _insert(self, device_id: str, etype: str, start_time: str, end_time: str | None,
                st: _DevState, t: float, detail: dict[str, Any] | None = None) -> int:
        d = detail or {}
        if etype in ("fall", "fall_suspect"):
            d.update(self._fall_detail(st))
        if st.last_pos:
            d["lon_bd"], d["lat_bd"] = st.last_pos
        eid = self.storage.insert_event(device_id, etype, start_time, end_time, d)
        logger.info("事件 %s %s %s %s", device_id, etype, start_time, d.get("direction", ""))
        return eid

    @staticmethod
    def _parse_time(gps_time: str) -> float:
        try:
            return time.mktime(time.strptime(gps_time, "%Y-%m-%d %H:%M:%S"))
        except (ValueError, OverflowError):
            return time.time()

    @staticmethod
    def _six_axis(point: dict[str, Any]) -> list[tuple[float, ...]] | None:
        src = point.get("gyro") if isinstance(point.get("gyro"), dict) else point
        vals = [src.get(k) for k in ("gyro_x", "gyro_y", "gyro_z", "acc_x", "acc_y", "acc_z")]
        if any(x is None for x in vals):
            return None
        nums = [float(x) for x in vals]
        return [tuple(nums[0:3]), tuple(nums[3:6])]
