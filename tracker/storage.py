"""SQLite 存储:设备表 + 轨迹点表。1Hz 写入量很小,同步 sqlite3 + 锁即可。"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any

DB_DIR = Path(__file__).resolve().parent.parent / "data"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS devices (
    device_id   TEXT PRIMARY KEY,
    registered_at REAL,
    last_seen   REAL,
    auth_code   TEXT DEFAULT '',
    plate       TEXT DEFAULT '',
    protocol    TEXT DEFAULT 'jt808'
);
CREATE TABLE IF NOT EXISTS track_points (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    device_id   TEXT NOT NULL,
    server_ts   REAL NOT NULL,
    gps_time    TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    lat_bd      REAL NOT NULL,
    lon_bd      REAL NOT NULL,
    speed       REAL DEFAULT 0,
    direction   INTEGER DEFAULT 0,
    altitude    INTEGER DEFAULT 0,
    acc_on      INTEGER DEFAULT 0,
    located     INTEGER DEFAULT 1,
    alarm       INTEGER DEFAULT 0,
    gyro_x      INTEGER, gyro_y INTEGER, gyro_z INTEGER,
    acc_x       INTEGER, acc_y  INTEGER, acc_z  INTEGER,
    extras      TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_track_device ON track_points(device_id, id);
"""


class Storage:
    def __init__(self, db_path: str | Path | None = None) -> None:
        DB_DIR.mkdir(parents=True, exist_ok=True)
        path = str(db_path or DB_DIR / "tracker.db")
        self._conn = sqlite3.connect(path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.executescript(_SCHEMA)
        try:  # 旧库升级
            self._conn.execute("ALTER TABLE devices ADD COLUMN protocol TEXT DEFAULT 'jt808'")
        except sqlite3.OperationalError:
            pass
        self._lock = threading.Lock()

    # ── 设备 ───────────────────────────────────────────

    def upsert_device(
        self,
        device_id: str,
        auth_code: str | None = None,
        plate: str | None = None,
        protocol: str | None = None,
    ) -> None:
        now = time.time()
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO devices(device_id, registered_at, last_seen, auth_code, plate, protocol)
                VALUES(?, ?, ?, COALESCE(?, ''), COALESCE(?, ''), COALESCE(?, 'jt808'))
                ON CONFLICT(device_id) DO UPDATE SET
                    last_seen = excluded.last_seen,
                    auth_code = COALESCE(?, devices.auth_code),
                    plate = COALESCE(?, devices.plate),
                    protocol = COALESCE(?, devices.protocol)
                """,
                (device_id, now, now, auth_code, plate, protocol, auth_code, plate, protocol),
            )
            self._conn.commit()

    def touch_device(self, device_id: str) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE devices SET last_seen = ? WHERE device_id = ?", (time.time(), device_id)
            )
            if self._conn.total_changes == 0:
                pass
            self._conn.commit()

    def get_auth_code(self, device_id: str) -> str | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT auth_code FROM devices WHERE device_id = ?", (device_id,)
            ).fetchone()
        return row["auth_code"] if row else None

    # ── 轨迹 ───────────────────────────────────────────

    def insert_point(self, device_id: str, point: dict[str, Any], lon_bd: float, lat_bd: float) -> None:
        gyro = point.get("gyro") or {}
        with self._lock:
            self._conn.execute(
                """
                INSERT INTO track_points(
                    device_id, server_ts, gps_time, lat, lon, lat_bd, lon_bd,
                    speed, direction, altitude, acc_on, located, alarm,
                    gyro_x, gyro_y, gyro_z, acc_x, acc_y, acc_z, extras
                ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                """,
                (
                    device_id,
                    time.time(),
                    point["gps_time"],
                    point["lat"],
                    point["lon"],
                    lat_bd,
                    lon_bd,
                    point["speed"],
                    point["direction"],
                    point["altitude"],
                    int(point["acc_on"]),
                    int(point["located"]),
                    point["alarm"],
                    gyro.get("gyro_x"),
                    gyro.get("gyro_y"),
                    gyro.get("gyro_z"),
                    gyro.get("acc_x"),
                    gyro.get("acc_y"),
                    gyro.get("acc_z"),
                    json.dumps(point.get("extras") or {}, ensure_ascii=False),
                ),
            )
            self._conn.execute(
                "UPDATE devices SET last_seen = ? WHERE device_id = ?", (time.time(), device_id)
            )
            self._conn.commit()

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._conn.execute(
                """
                SELECT d.device_id, d.last_seen, d.plate, d.protocol,
                       p.lat_bd, p.lon_bd, p.speed, p.direction, p.gps_time,
                       (SELECT COUNT(*) FROM track_points t WHERE t.device_id = d.device_id) AS point_count
                FROM devices d
                LEFT JOIN track_points p ON p.id = (
                    SELECT id FROM track_points WHERE device_id = d.device_id ORDER BY id DESC LIMIT 1
                )
                ORDER BY d.last_seen DESC
                """
            ).fetchall()
        now = time.time()
        return [
            {
                **dict(r),
                "online": (now - (r["last_seen"] or 0)) < 30,
            }
            for r in rows
        ]

    def recent_located_before(
        self, device_id: str, point_id: int, limit: int = 15
    ) -> list[dict[str, Any]]:
        """id <= point_id 的最近若干个有效定位点(升序),供增量查询做运动状态判定。"""
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM track_points WHERE device_id = ? AND located = 1 AND id <= ?"
                " ORDER BY id DESC LIMIT ?",
                (device_id, point_id, limit),
            ).fetchall()
        return [self._point_row(r) for r in reversed(rows)]

    def latest_point(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._conn.execute(
                "SELECT * FROM track_points WHERE device_id = ? ORDER BY id DESC LIMIT 1",
                (device_id,),
            ).fetchone()
        return self._point_row(row) if row else None

    def track(
        self,
        device_id: str,
        since_id: int = 0,
        start: str | None = None,
        end: str | None = None,
        limit: int = 5000,
        only_located: bool = False,
    ) -> list[dict[str, Any]]:
        sql = "SELECT * FROM track_points WHERE device_id = ?"
        args: list[Any] = [device_id]
        if only_located:
            sql += " AND located = 1"
        if since_id:
            sql += " AND id > ?"
            args.append(since_id)
        if start:
            sql += " AND gps_time >= ?"
            args.append(start)
        if end:
            sql += " AND gps_time <= ?"
            args.append(end)
        sql += " ORDER BY id ASC LIMIT ?"
        args.append(max(1, min(limit, 50000)))
        with self._lock:
            rows = self._conn.execute(sql, args).fetchall()
        return [self._point_row(r) for r in rows]

    @staticmethod
    def _point_row(row: sqlite3.Row) -> dict[str, Any]:
        d = dict(row)
        d["extras"] = json.loads(d.get("extras") or "{}")
        return d
